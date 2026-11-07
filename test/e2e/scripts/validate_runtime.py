#!/usr/bin/env python3
"""Operator-run functional validation. Mutates only a fresh owned namespace.

Requires an explicit kubeconfig, context and expected node. Never registers a
runtime, changes a machine configuration, or reboots a node.
"""
import argparse
import hashlib
import io
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
from install_test_tools import host_lock


def kind_configuration(pod_subnet, service_subnet, outer_networks):
    """Keep the nested cluster from intercepting its outer DNS/routing ranges."""
    pod, service = (ipaddress.ip_network(value, strict=True) for value in (pod_subnet, service_subnet))
    if pod.version != 4 or service.version != 4 or pod.overlaps(service):
        raise ValueError('kind requires distinct IPv4 Pod and Service CIDRs')
    if not outer_networks:
        raise ValueError('outer network inventory is required')
    for value in outer_networks:
        outer = ipaddress.ip_network(value, strict=False)
        if outer.version == 4 and (pod.overlaps(outer) or service.overlaps(outer)):
            raise ValueError('nested kind CIDR overlaps an outer cluster or Docker network')
    return {'kind': 'Cluster', 'apiVersion': 'kind.x-k8s.io/v1alpha4',
            'networking': {'apiServerAddress': '127.0.0.1', 'ipFamily': 'ipv4',
                           'podSubnet': str(pod), 'serviceSubnet': str(service)},
            'nodes': [{'role': 'control-plane'}]}


def cluster_pod_networks(controller_pods):
    # Node.spec.podCIDRs covers allocations, not the complete cluster range.
    networks=set()
    for pod in controller_pods:
        for container in pod['spec']['containers']:
            args=(container.get('command') or [])+(container.get('args') or [])
            for index,arg in enumerate(args):
                if arg=='--cluster-cidr':
                    if index+1==len(args):raise ValueError('missing controller cluster CIDR')
                    value=args[index+1]
                elif arg.startswith('--cluster-cidr='):
                    value=arg.split('=',1)[1]
                else:
                    continue
                networks.update(str(ipaddress.ip_network(cidr,strict=True)) for cidr in value.split(','))
    if not networks:raise ValueError('controller-manager cluster CIDR inventory is required')
    return sorted(networks)


def verify_clients():
    # Client CPU architecture is the executor's, independent of the target node.
    lock = host_lock()
    selected = {'kubectl': lock['test_tools']['outer_kubectl'], 'talosctl': lock['test_tools']['talosctl']}
    hashes = {}
    for name, spec in selected.items():
        binary = shutil.which(name)
        if not binary or hashlib.sha256(Path(binary).read_bytes()).hexdigest() != spec['sha256']:
            raise RuntimeError('validation client differs from the host-architecture lock: '+name)
        hashes[name] = spec['sha256']
    return {'architecture': lock['architecture'], 'sha256': hashes}


def verify_source_inputs(metadata, selected_lock):
    for name,sha in metadata['source_inputs_sha256'].items():
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise RuntimeError('invalid source input path')
        path=selected_lock if name=='versions.lock.json' else ROOT/relative
        if hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
            raise RuntimeError('artifact source inputs differ from selected inputs; rebuild first')


def installed_hashes(metadata):
    """Exact enabled production inventory for API reads and supporting checks."""
    integration = metadata.get('proc_transfer', {})
    if integration.get('build_profile') != 'proc-sysfs-fuse-v1':
        raise RuntimeError('production build profile mismatch')
    binaries = metadata['binary_sha256']
    daemons = ('sysbox-runc', 'sysbox-mgr', 'sysbox-fs')
    helpers = ('proc-mount-helper', 'sysfs-mount-helper')
    outputs = integration.get('output_sha256', {})
    if (set(binaries) != set(daemons + helpers) or set(outputs) != {'sysbox-fs', *helpers}
            or any(outputs[name] != binaries[name] for name in outputs)):
        raise RuntimeError('production triple inventory mismatch')
    expected = {'/usr/local/bin/' + name: binaries[name] for name in daemons}
    for name in ('sysbox-fs', *helpers):
        expected['/usr/local/lib/containers/sysbox-fs/' + name] = binaries[name]
    for daemon in ('sysbox-mgr', 'sysbox-fs'):
        expected['/usr/local/lib/containers/' + daemon + '/supervisor'] = metadata['supervisor_sha256']
    return expected


class Runner:
    def __init__(self, args):
        self.args = args
        self.lock = json.loads(args.lock.read_text())
        if self.lock['architecture'] not in ('amd64', 'arm64'):
            raise ValueError('unsupported target architecture')
        self.namespace = 'sysbox-validation-'+uuid.uuid4().hex[:12]
        self.env = dict(os.environ, KUBECONFIG=str(args.kubeconfig.resolve()))
        self.command = ['kubectl','--kubeconfig',str(args.kubeconfig.resolve()),'--context',args.context,'--request-timeout=30s']
        self.private_logs=ROOT/'.tmp/e2e'/('runtime-'+self.namespace)
        self.private_logs.mkdir(parents=True,mode=0o700,exist_ok=True)
        self.command += ['--cache-dir',str(self.private_logs/'kubectl-cache')]
        self.sequence=0

    def k(self, args, data=None, timeout=180):
        p = subprocess.run([*self.command,*args], input=data, capture_output=True, env=self.env, timeout=timeout)
        self.sequence+=1
        log=self.private_logs/f'{self.sequence:03d}.log'
        log.write_bytes(p.stdout+b'\nSTDERR\n'+p.stderr);log.chmod(0o600)
        if p.returncode:
            # Raw API errors may contain node identities or addresses. Preserve
            # privately at the operator's discretion; never publish here.
            raise RuntimeError('kubectl operation failed: '+args[0])
        return p.stdout

    def execute(self, args, data=None, timeout=180):
        return self.k(['-n',self.namespace,'exec','-i','docker','--',*args],data,timeout)

    def docker(self, args, timeout=180):
        return self.execute(['docker','-H','unix:///run/docker.sock',*args],timeout=timeout)

    def ready(self):
        deadline = time.monotonic()+90
        while time.monotonic()<deadline:
            try:
                info = json.loads(self.docker(['info','--format','{{json .}}']).decode())
                if info['Driver']!='overlay2' or info['CgroupDriver']!='cgroupfs' or info['CgroupVersion']!='2':
                    raise ValueError('unexpected Docker storage/cgroup configuration')
                return info
            except RuntimeError:
                time.sleep(2)
        raise RuntimeError('Docker readiness deadline exceeded')

    def child(self):
        self.docker(['run','--rm','--privileged','--network=none','--cpus=1','--memory=64m','--pids-limit=64',self.lock['test_images']['busybox'],
                     'sh','-ceu','mkdir /tmp/sysbox-child; mount -t tmpfs tmpfs /tmp/sysbox-child; '
                     'echo child-ready > /tmp/sysbox-child/check; test "$(cat /tmp/sysbox-child/check)" = child-ready; '
                     'umount /tmp/sysbox-child; rmdir /tmp/sysbox-child'])

    def status(self):
        pod = json.loads(self.k(['-n',self.namespace,'get','pod','docker','-o','json']))
        return pod['status']['containerStatuses'][0]

    def upload(self, path, data):
        self.execute(['sh','-ceu','cat > "$1"; chmod 755 "$1"','upload',path],data)

    def tools(self):
        self.execute(['mkdir','-p','/workspace'])
        for name,spec in self.lock['test_tools'].items():
            if name not in ('kubectl','kind','helm'): continue
            data=urllib.request.urlopen(spec['url'],timeout=90).read()
            if hashlib.sha256(data).hexdigest()!=spec['sha256']:
                raise RuntimeError('test tool checksum mismatch')
            if name=='helm':
                with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                    data=tf.extractfile('linux-'+self.lock['architecture']+'/helm').read()
            self.upload('/usr/local/bin/'+name,data)

    def run(self):
        metadata=json.loads(self.args.build_metadata.read_text())
        if metadata.get('build_mode')!='release':
            raise RuntimeError('only a completed production build may be validated')
        expected = installed_hashes(metadata)
        verify_source_inputs(metadata,self.args.lock)
        clients = verify_clients()
        nodes=json.loads(self.k(['get','nodes','-o','json']))['items']
        matches=[n for n in nodes if n['metadata']['name']==self.args.expected_node]
        if len(matches)!=1:
            raise RuntimeError('expected node not found; refusing mutation')
        node=matches[0]
        info=node['status']['nodeInfo']
        if info['architecture']!=self.lock['architecture'] or info['osImage']!='Talos (v'+self.lock['talos']+')':
            raise RuntimeError('target is not locked '+self.lock['architecture']+' Talos')
        if self.args.environment=='real-talos' and not info['kernelVersion'].endswith('-talos'):
            raise RuntimeError('real-talos requires an actual Talos kernel')
        if self.args.environment=='real-talos':
            if not all((self.args.talosconfig,self.args.talos_context,self.args.endpoint,self.args.node_address)):
                raise RuntimeError('real Talos validation requires explicit Talos API connection inputs for installed-artifact verification')
            connection=['talosctl','--talosconfig',str(self.args.talosconfig.resolve()),'--context',self.args.talos_context,'--endpoints',self.args.endpoint,'--nodes',self.args.node_address]
            for path,sha in expected.items():
                response=subprocess.run([*connection,'read',path],capture_output=True,timeout=45)
                if response.returncode or hashlib.sha256(response.stdout).hexdigest()!=sha:
                    raise RuntimeError('installed runtime artifact does not match this build')
        rc=json.loads(self.k(['get','runtimeclass','sysbox-runc','-o','json']))
        if rc['handler']!='sysbox-runc': raise RuntimeError('unexpected RuntimeClass')
        outer_networks = [cidr for item in nodes for cidr in item['spec'].get('podCIDRs', [item['spec'].get('podCIDR')]) if cidr]
        services = json.loads(self.k(['get','servicecidrs.networking.k8s.io','-o','json']))['items']
        service_networks = [cidr for item in services for cidr in item['spec']['cidrs']]
        if not service_networks or not outer_networks:
            raise RuntimeError('outer Pod and Service CIDR inventory is required before validation')
        controllers=json.loads(self.k(['-n','kube-system','get','pods','-l','component=kube-controller-manager','-o','json']))['items']
        cluster_networks=cluster_pod_networks(controllers)
        configured=[ipaddress.ip_network(value) for value in cluster_networks]
        if any(not any(ipaddress.ip_network(value).version==network.version and ipaddress.ip_network(value).subnet_of(network) for network in configured) for value in outer_networks):
            raise RuntimeError('allocated Pod CIDR is outside the controller cluster CIDRs')
        outer_networks += cluster_networks+service_networks
        outer_networks += [item['address'] for item in node['status']['addresses'] if item['type'] in ('InternalIP', 'ExternalIP')]
        kind_config = kind_configuration(self.args.kind_pod_subnet, self.args.kind_service_subnet, outer_networks)
        result={'build_profile': metadata['proc_transfer']['build_profile'], 'environment':self.args.environment,'talos':self.lock['talos'],'sysbox':self.lock['sysbox'],'kernel':info['kernelVersion'],'architecture':self.lock['architecture'],'runtime_binary_sha256':metadata['binary_sha256'],'supervisor_sha256':metadata['supervisor_sha256'],'checks':{},'cleanup':'unrun','passed':False}
        result.update(source_inputs_sha256=metadata['source_inputs_sha256'], validation_clients=clients,
                      outer_kubelet_version=info['kubeletVersion'],
                      validation_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      inner_networking=kind_config['networking'])
        created=False
        try:
            ns={'apiVersion':'v1','kind':'Namespace','metadata':{'name':self.namespace,'labels':{'pod-security.kubernetes.io/enforce':'privileged'}}}
            self.k(['create','-f','-'],json.dumps(ns).encode()); created=True
            pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':'docker','namespace':self.namespace},'spec':{'nodeName':self.args.expected_node,'runtimeClassName':'sysbox-runc','hostUsers':False,'automountServiceAccountToken':False,'restartPolicy':'Always','tolerations':[{'operator':'Exists'}],'containers':[{'name':'docker','image':self.lock['test_images']['docker'],'command':['sh','-ceu','exec dockerd --host=unix:///run/docker.sock --storage-driver=overlay2 --exec-opt native.cgroupdriver=cgroupfs'],'securityContext':{'runAsUser':0,'runAsGroup':0,'privileged':False},'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'2','memory':'4Gi'}}}]}}
            self.k(['create','-f','-'],json.dumps(pod).encode())
            self.k(['-n',self.namespace,'wait','--for=condition=Ready','pod/docker','--timeout=120s'])
            docker_info=self.ready(); result['checks']['direct_rootful_docker']=True
            result['docker']={key:docker_info[key] for key in ('ServerVersion','Driver','CgroupDriver','CgroupVersion','Architecture')}
            self.execute(['sh','-ceu','test "$(id -u)" = 0; test "$(id -g)" = 0'])
            mapping=[tuple(map(int,line.split())) for line in self.execute(['cat','/proc/self/uid_map']).decode().splitlines()]
            if (not mapping or any(len(row)!=3 or row[1]==0 for row in mapping)
                    or not any(row[0]==0 and row[1]>0 and row[2]>=65536 for row in mapping)):
                raise RuntimeError('Docker root lacks a noninitial user namespace')
            security=dict(line.split(':',1) for line in self.execute(['cat','/proc/self/status']).decode().splitlines() if ':' in line)
            if security['Seccomp'].strip()!='2':
                raise RuntimeError('Docker outer container lost the production seccomp filter')
            result['checks']['docker_mapped_root_and_production_seccomp']=True
            self.child(); result['checks']['privileged_child']=True
            self.tools()
            # Include currently allocated inner-Docker ranges as well; never
            # change the daemon's network configuration to make a check pass.
            networks = [json.loads(line) for line in self.docker(['network','ls','--format','{{json .ID}}']).decode().splitlines() if line]
            allocated = json.loads(self.docker(['network','inspect',*networks]))
            docker_ranges = [entry['Subnet'] for network in allocated for entry in (network.get('IPAM',{}).get('Config') or []) if entry.get('Subnet')]
            kind_configuration(self.args.kind_pod_subnet, self.args.kind_service_subnet, outer_networks+docker_ranges)
            self.execute(['sh','-ceu','cat > /workspace/kind.json'],json.dumps(kind_config).encode())
            result['checks']['nonoverlapping_kind_networks']=True
            script=f'''set -eu
export KUBECONFIG=/workspace/kind.kubeconfig
export DOCKER_HOST=unix:///run/docker.sock
kind create cluster --name sysbox-validation --config /workspace/kind.json --image {self.lock['test_images']['kind_node']} --kubeconfig "$KUBECONFIG" --wait 180s
kubectl wait --for=condition=Ready nodes --all --timeout=180s
kubectl -n kube-system rollout status deployment/coredns --timeout=180s
echo SYSBOX_KIND_READY
# Preload the digest-verified outer image to avoid a second registry lookup.
# The smoke test still exercises Kubernetes Service DNS and TCP below.
docker tag {self.lock['test_images']['busybox']} sysbox-smoke:locked
kind load docker-image sysbox-smoke:locked --name sysbox-validation
mkdir -p /workspace/chart/templates
printf 'apiVersion: v2\nname: smoke\nversion: 0.1.0\n' > /workspace/chart/Chart.yaml
cat > /workspace/chart/templates/resources.yaml <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: smoke
spec:
  replicas: 1
  selector:
    matchLabels: {{app: smoke}}
  template:
    metadata:
      labels: {{app: smoke}}
    spec:
      containers:
        - name: http
          image: sysbox-smoke:locked
          imagePullPolicy: Never
          command: [sh, -ceu, 'mkdir -p /www; echo sysbox-ready > /www/index.html; exec httpd -f -p 8080 -h /www']
---
apiVersion: v1
kind: Service
metadata:
  name: smoke
spec:
  selector: {{app: smoke}}
  ports:
    - port: 8080
      targetPort: 8080
YAML
helm upgrade --install smoke /workspace/chart --namespace smoke --create-namespace --wait --timeout 180s
kubectl -n smoke rollout status deployment/smoke --timeout=180s
echo SYSBOX_HELM_ROLLOUT
kubectl -n smoke exec deployment/smoke -- sh -ceu 'test "$(wget -qO- http://smoke.smoke.svc.cluster.local:8080)" = sysbox-ready'
echo SYSBOX_SERVICE_DNS_TCP
kubectl -n smoke exec deployment/smoke -- nslookup registry.k8s.io
echo SYSBOX_UPSTREAM_DNS
kind delete cluster --name sysbox-validation --kubeconfig "$KUBECONFIG"
'''
            self.execute(['sh','-s'],script.encode(),timeout=900)
            result['checks'].update(kind_ready=True,helm_rollout=True,service_dns_tcp=True,upstream_dns=True)
            before=self.status()
            try: self.execute(['sh','-c','kill -TERM 1'])
            except RuntimeError: pass  # exec may disconnect as PID 1 exits.
            deadline=time.monotonic()+150
            while time.monotonic()<deadline:
                try: after=self.status()
                except (RuntimeError,KeyError):
                    time.sleep(2)
                    continue
                if after.get('ready') and after.get('containerID')!=before.get('containerID') and after.get('restartCount',0)>before.get('restartCount',0):
                    break
                time.sleep(2)
            else: raise RuntimeError('container recreation deadline exceeded')
            self.ready(); self.child()
            result['checks']['recreated_id_and_child']=True
            # No RuntimeClass means default runtime. The identity map proves
            # this ordinary Pod did not acquire Sysbox's user namespace.
            normal={'apiVersion':'v1','kind':'Pod','metadata':{'name':'ordinary','namespace':self.namespace},'spec':{'nodeName':self.args.expected_node,'hostUsers':True,'automountServiceAccountToken':False,'restartPolicy':'Never','tolerations':[{'operator':'Exists'}],'containers':[{'name':'ordinary','image':self.lock['test_images']['busybox'],'command':['sh','-ceu','test "$(id -u)" = 1001; test "$(awk \'{print $1,$2,$3}\' /proc/self/uid_map)" = "0 0 4294967295"; test "$(awk \'/^CapEff:/ {print $2}\' /proc/self/status)" = 0000000000000000; test "$(awk \'/^NoNewPrivs:/ {print $2}\' /proc/self/status)" = 1; test "$(awk \'/^Seccomp:/ {print $2}\' /proc/self/status)" = 2'],'securityContext':{'runAsUser':1001,'runAsGroup':1001,'runAsNonRoot':True,'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']},'seccompProfile':{'type':'RuntimeDefault'}}}]}}
            self.k(['create','-f','-'],json.dumps(normal).encode())
            self.k(['-n',self.namespace,'wait','--for=jsonpath={.status.phase}=Succeeded','pod/ordinary','--timeout=120s'])
            result['checks']['ordinary_runtime_healthy']=True
            result['passed']=True
        finally:
            if created:
                try:
                    self.k(['delete','namespace',self.namespace,'--wait=true','--timeout=120s'])
                    result['cleanup']='passed'
                except Exception:
                    result['cleanup']='failed'; result['passed']=False
            self.args.result.parent.mkdir(parents=True,exist_ok=True)
            self.args.result.write_text(json.dumps(result,indent=2)+'\n')
        if not result['passed']: raise RuntimeError('validation failed')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--kubeconfig',type=Path,required=True)
    p.add_argument('--context',required=True)
    p.add_argument('--expected-node',required=True)
    p.add_argument('--environment',choices=('real-talos','talos-in-docker'),required=True)
    p.add_argument('--result',type=Path,required=True)
    p.add_argument('--build-metadata',type=Path,required=True)
    p.add_argument('--lock',type=Path,default=ROOT/'versions.lock.json')
    p.add_argument('--talosconfig',type=Path)
    p.add_argument('--talos-context')
    p.add_argument('--endpoint')
    p.add_argument('--node-address')
    p.add_argument('--kind-pod-subnet',default='10.200.0.0/16')
    p.add_argument('--kind-service-subnet',default='10.201.0.0/16')
    a=p.parse_args()
    if not a.kubeconfig.is_file(): p.error('explicit kubeconfig is required')
    Runner(a).run()


if __name__=='__main__':
    try: main()
    except Exception as error:
        # Exceptions such as TimeoutExpired include the full private argv.
        # Raw details belong only to the private per-command diagnostics.
        print('Runtime validation failed ('+type(error).__name__+'); inspect the sanitized result and private diagnostics.')
        raise SystemExit(1)
