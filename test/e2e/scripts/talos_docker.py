#!/usr/bin/env python3
"""Supporting host-kernel validation in an owned, disposable Talos cluster."""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('runtime_patch', ROOT/'scripts/runtime_patch.py')
runtime=importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--rootfs',required=True,type=Path)
    p.add_argument('--subnet',required=True)
    p.add_argument('--constrained-disk',action='store_true',help='Docker test nodes only: allow 5%% free disk, 95%% image GC threshold')
    p.add_argument('--result',type=Path,default=ROOT/'test/e2e/results/talos-in-docker.json')
    a=p.parse_args()
    if not Path('/sys/module/br_netfilter').exists():
        raise RuntimeError('host br_netfilter must be loaded explicitly by the operator before starting Talos-in-Docker')
    lock=json.loads((ROOT/'versions.lock.json').read_text())
    name='sysbox-ci-'+uuid.uuid4().hex[:10]
    if subprocess.run(['docker','network','inspect',name],capture_output=True).returncode==0:
        raise RuntimeError('owned test network name already exists')
    image=name+':v'+lock['talos']
    subprocess.run(['docker','pull',lock['test_images']['talos_docker']],capture_output=True,check=True,timeout=180)
    subprocess.run(['docker','tag',lock['test_images']['talos_docker'],image],check=True)
    private=ROOT/'.tmp/e2e'/name; private.mkdir(parents=True,mode=0o700)
    local=private/'local'
    shutil.copytree(a.rootfs/'usr/local',local)
    (local/'lib/kubelet/credentialproviders').mkdir(parents=True,exist_ok=True)
    for daemon in ('sysbox-mgr','sysbox-fs'):
        for d in ('run','var','dev','proc','sys'):
            (local/'lib/containers'/daemon/d).mkdir(exist_ok=True)
    kube=private/'kubeconfig'; talos=private/'talosconfig'
    env=dict(os.environ,KUBECONFIG=str(kube),TALOSCONFIG=str(talos))
    patch={'machine':{'files':[{'path':'/etc/cri/conf.d/20-customization.part','permissions':420,'op':'create','content':runtime.merge('')}]},'cluster':{'allowSchedulingOnControlPlanes':True}}
    if a.constrained_disk:
        patch['machine']['kubelet']={'extraArgs':{'eviction-hard':'nodefs.available<5%,imagefs.available<5%,nodefs.inodesFree<5%','eviction-pressure-transition-period':'10s','image-gc-high-threshold':'95','image-gc-low-threshold':'90'}}
    patch_path=private/'patch.json'; patch_path.write_text(json.dumps(patch)); patch_path.chmod(0o600)
    base=['talosctl','cluster','--name',name,'--state',str(private/'state')]
    cleanup=False
    outcome={'environment':'talos-in-docker','passed':False,'cleanup':'unrun','checks':{}}
    try:
        with (private/'private.log').open('wb') as log:
            subprocess.run([*base,'create','docker','--workers','0','--image',image,'--kubernetes-version','1.35.2','--memory-controlplanes','6GiB','--cpus-controlplanes','4','--subnet',a.subnet,'--talosconfig-destination',str(talos),'--config-patch','@'+str(patch_path),'--mount','type=bind,src='+str(local)+',dst=/usr/local'],env=env,stdout=log,stderr=log,check=True,timeout=600)
        context=subprocess.check_output(['kubectl','--kubeconfig',str(kube),'config','current-context'],env=env,text=True).strip()
        subprocess.run(['kubectl','--kubeconfig',str(kube),'--context',context,'apply','-f',str(ROOT/'examples/runtimeclass.yaml')],env=env,capture_output=True,check=True,timeout=60)
        subprocess.run(['python3',str(ROOT/'test/e2e/scripts/validate_runtime.py'),'--kubeconfig',str(kube),'--context',context,'--expected-node',name+'-controlplane-1','--environment','talos-in-docker','--result',str(a.result)],env=env,check=True,timeout=1500)
        outcome=json.loads(a.result.read_text())
    finally:
        with (private/'cleanup.log').open('wb') as log:
            r=subprocess.run([*base,'destroy'],env=env,stdout=log,stderr=log,timeout=180)
            cleanup=r.returncode==0
        if not cleanup:
            # cluster create can fail before persisting its state. Remove only
            # the exact node backed by our unique temporary image alias.
            node=name+'-controlplane-1'
            inspect=subprocess.run(['docker','inspect',node],capture_output=True)
            if inspect.returncode==0:
                data=json.loads(inspect.stdout)[0]
                if data['Config']['Image']!=image:
                    raise RuntimeError('unexpected node ownership; refusing cleanup')
                subprocess.run(['docker','rm','-f',data['Id']],capture_output=True,check=True)
            network=subprocess.run(['docker','network','inspect',name],capture_output=True)
            if network.returncode==0:
                subprocess.run(['docker','network','rm',name],capture_output=True,check=True)
            cleanup=True
        outcome['cluster_cleanup']='passed' if cleanup else 'failed'
        outcome['passed']=outcome['passed'] and cleanup
        subprocess.run(['docker','image','rm',image],capture_output=True,timeout=60)
        a.result.parent.mkdir(parents=True,exist_ok=True)
        a.result.write_text(json.dumps(outcome,indent=2)+'\n')
        if cleanup: shutil.rmtree(private)
    if not outcome['passed']: raise RuntimeError('isolated Talos validation failed; inspect sanitized result')


if __name__=='__main__':
    try: main()
    except Exception as error:
        print('Talos-in-Docker validation failed ('+type(error).__name__+'); private diagnostics are retained when cleanup fails.')
        raise SystemExit(1)
