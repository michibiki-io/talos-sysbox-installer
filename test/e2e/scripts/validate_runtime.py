#!/usr/bin/env python3
"""Operator-run functional validation. Mutates only a fresh owned namespace.

Requires an explicit kubeconfig, context and expected node. Never registers a
runtime, changes a machine configuration, or reboots a node.
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class Runner:
    def __init__(self, args):
        self.args = args
        self.lock = json.loads((ROOT/'versions.lock.json').read_text())
        self.namespace = 'sysbox-validation-'+uuid.uuid4().hex[:12]
        self.env = dict(os.environ, KUBECONFIG=str(args.kubeconfig.resolve()))
        self.command = ['kubectl','--kubeconfig',str(args.kubeconfig.resolve()),'--context',args.context,'--request-timeout=30s']
        self.private_logs=ROOT/'.tmp/e2e'/('runtime-'+self.namespace)
        self.private_logs.mkdir(parents=True,mode=0o700,exist_ok=True)
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
                return
            except RuntimeError:
                time.sleep(2)
        raise RuntimeError('Docker readiness deadline exceeded')

    def child(self):
        self.docker(['run','--rm','--privileged','--network=none',self.lock['test_images']['busybox'],'true'])

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
                    data=tf.extractfile('linux-amd64/helm').read()
            self.upload('/usr/local/bin/'+name,data)

    def run(self):
        metadata=json.loads(self.args.build_metadata.read_text())
        if metadata.get('build_mode')!='release':
            raise RuntimeError('only a completed production build may be validated')
        for name,sha in metadata['source_inputs_sha256'].items():
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise RuntimeError('invalid source input path')
            if hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()!=sha:
                raise RuntimeError('artifact source inputs differ from current checkout; rebuild first')
        nodes=json.loads(self.k(['get','nodes','-o','json']))['items']
        matches=[n for n in nodes if n['metadata']['name']==self.args.expected_node]
        if len(matches)!=1:
            raise RuntimeError('expected node not found; refusing mutation')
        node=matches[0]
        info=node['status']['nodeInfo']
        if info['architecture']!='amd64' or info['osImage']!='Talos (v'+self.lock['talos']+')':
            raise RuntimeError('target is not locked amd64 Talos')
        if self.args.environment=='real-talos' and not info['kernelVersion'].endswith('-talos'):
            raise RuntimeError('real-talos requires an actual Talos kernel')
        if self.args.environment=='real-talos':
            if not all((self.args.talosconfig,self.args.talos_context,self.args.endpoint,self.args.node_address)):
                raise RuntimeError('real Talos validation requires explicit Talos API connection inputs for installed-artifact verification')
            connection=['talosctl','--talosconfig',str(self.args.talosconfig.resolve()),'--context',self.args.talos_context,'--endpoints',self.args.endpoint,'--nodes',self.args.node_address]
            expected={('/usr/local/bin/'+name):sha for name,sha in metadata['binary_sha256'].items()}
            for daemon in ('sysbox-mgr','sysbox-fs'):
                expected['/usr/local/lib/containers/'+daemon+'/supervisor']=metadata['supervisor_sha256']
            for path,sha in expected.items():
                response=subprocess.run([*connection,'read',path],capture_output=True,timeout=45)
                if response.returncode or hashlib.sha256(response.stdout).hexdigest()!=sha:
                    raise RuntimeError('installed runtime artifact does not match this build')
        rc=json.loads(self.k(['get','runtimeclass','sysbox-runc','-o','json']))
        if rc['handler']!='sysbox-runc': raise RuntimeError('unexpected RuntimeClass')
        result={'environment':self.args.environment,'talos':self.lock['talos'],'sysbox':self.lock['sysbox'],'kernel':info['kernelVersion'],'architecture':'amd64','runtime_binary_sha256':metadata['binary_sha256'],'supervisor_sha256':metadata['supervisor_sha256'],'checks':{},'cleanup':'unrun','passed':False}
        created=False
        try:
            ns={'apiVersion':'v1','kind':'Namespace','metadata':{'name':self.namespace,'labels':{'pod-security.kubernetes.io/enforce':'privileged'}}}
            self.k(['create','-f','-'],json.dumps(ns).encode()); created=True
            pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':'docker','namespace':self.namespace},'spec':{'nodeName':self.args.expected_node,'runtimeClassName':'sysbox-runc','hostUsers':False,'automountServiceAccountToken':False,'restartPolicy':'Always','tolerations':[{'operator':'Exists'}],'containers':[{'name':'docker','image':self.lock['test_images']['docker'],'command':['sh','-ceu','exec dockerd --host=unix:///run/docker.sock --storage-driver=overlay2 --exec-opt native.cgroupdriver=cgroupfs'],'securityContext':{'runAsUser':0,'runAsGroup':0,'privileged':False},'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'2','memory':'4Gi'}}}]}}
            self.k(['create','-f','-'],json.dumps(pod).encode())
            self.k(['-n',self.namespace,'wait','--for=condition=Ready','pod/docker','--timeout=120s'])
            self.ready(); result['checks']['direct_rootful_docker']=True
            self.child(); result['checks']['privileged_child']=True
            self.tools()
            script=f'''set -eu
export KUBECONFIG=/workspace/kind.kubeconfig
export DOCKER_HOST=unix:///run/docker.sock
kind create cluster --name sysbox-validation --image {self.lock['test_images']['kind_node']} --kubeconfig "$KUBECONFIG" --wait 180s
kubectl wait --for=condition=Ready nodes --all --timeout=180s
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
kind delete cluster --name sysbox-validation --kubeconfig "$KUBECONFIG"
'''
            self.execute(['sh','-s'],script.encode(),timeout=900)
            result['checks'].update(kind_ready=True,helm_rollout=True,service_dns_tcp=True)
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
            normal={'apiVersion':'v1','kind':'Pod','metadata':{'name':'ordinary','namespace':self.namespace},'spec':{'nodeName':self.args.expected_node,'automountServiceAccountToken':False,'restartPolicy':'Never','tolerations':[{'operator':'Exists'}],'containers':[{'name':'ordinary','image':self.lock['test_images']['busybox'],'command':['sh','-ceu','test "$(awk \'{print $1,$2,$3}\' /proc/self/uid_map)" = "0 0 4294967295"']}]}}
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
    p.add_argument('--talosconfig',type=Path)
    p.add_argument('--talos-context')
    p.add_argument('--endpoint')
    p.add_argument('--node-address')
    a=p.parse_args()
    if not a.kubeconfig.is_file(): p.error('explicit kubeconfig is required')
    Runner(a).run()


if __name__=='__main__': main()
