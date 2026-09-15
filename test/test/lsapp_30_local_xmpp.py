#!/usr/bin/env python3
"""Create localhost-only synthetic chat fixtures; no external chat accounts."""
import argparse
import os
from pathlib import Path
import subprocess

USERS = ['pidgin','gajim','dino','psi','kaidan','echo']
PASSWORD = 'parp-synthetic-local-only'


def prepare(out):
    out.mkdir(parents=True,exist_ok=True)
    (out/'data').mkdir(exist_ok=True)
    cert,key=out/'localhost.crt',out/'localhost.key'
    if not key.exists():
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','7',
            '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1',
            '-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    config=out/'prosody.cfg.lua'
    config.write_text(f'''daemonize = false
pidfile = "{out}/prosody.pid"
data_path = "{out}/data"
interfaces = {{ "127.0.0.1" }}
c2s_ports = {{ 5222 }}
s2s_ports = {{}}
http_ports = {{}}
https_ports = {{}}
modules_enabled = {{ "roster", "saslauth", "tls", "disco", "private", "vcard", "version", "ping", "time", "pep", "carbons", "mam", "blocklist", "smacks" }}
log = {{ info = "{out}/server.log"; error = "{out}/error.log" }}
ssl = {{ key = "{key}"; certificate = "{cert}" }}
c2s_require_encryption = false
allow_unencrypted_plain_auth = true
VirtualHost "localhost"
authentication = "internal_plain"
Component "conference.localhost" "muc"
restrict_room_creation = "local"
''')
    for name in USERS:
        subprocess.run(['prosodyctl','--config',str(config),'register',name,'localhost',PASSWORD],check=True)
    roster=out/'data/localhost/roster';roster.mkdir(parents=True,exist_ok=True)
    for name in USERS:
        other='pidgin' if name=='echo' else 'echo'
        (roster/(name+'.dat')).write_text('return { ["'+other+'@localhost"] = { subscription="both"; name="PARP Echo"; groups={ ["PARP"]=true; }; }; [false]={ version=1; pending={}; }; }\n')
    return config


def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','server','echo'])
    parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args();out=args.output_dir.resolve()
    if args.command=='prepare':print(prepare(out))
    elif args.command=='server':os.execvp('prosody',['prosody','--config',str(out/'prosody.cfg.lua'),'-F'])
    else:
        # Ubuntu's packaged SleekXMPP predates Python 3.10's ABC relocation.
        import collections
        import collections.abc
        if not hasattr(collections, 'MutableSet'):
            collections.MutableSet = collections.abc.MutableSet
        import sleekxmpp
        client=sleekxmpp.ClientXMPP('echo@localhost',PASSWORD)
        def start(event):
            client.send_presence();client.get_roster()
        def message(event):
            if event['type'] in ('chat','normal') and event['body']:
                body=str(event['body'])
                with (out/'echo-received.log').open('a') as f:f.write(str(event['from'])+' '+body+'\n')
                event.reply('PARP received: '+body).send()
        client.add_event_handler('session_start',start)
        client.add_event_handler('message',message)
        if not client.connect(address=('127.0.0.1',5222),use_tls=False):raise RuntimeError('local XMPP connection failed')
        client.process(block=True)


if __name__=='__main__':main()
