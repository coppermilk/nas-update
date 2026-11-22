"""Политика source=git: настоящий рендер в chroot + build_deploy на «злых» вариантах.

Нужны root (chroot) и закреплённый compose: COMPOSE_BIN=/путь/docker-compose.
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from git_source import NNP, Policy, run_as_volumes  # noqa: E402

DC = os.environ['COMPOSE_BIN']
FIX = os.path.join(HERE, 'fixtures', 'repo')
REPO = '/volume1/docker/minion2'
DATA = '/volume1/homes/user/Main/Drive'
JAIL = '/tmp/nas-update-policy-jail'
USER = '1035:100'
policy = Policy('minion2', 'ghcr.io/coppermilk/minion2:latest', ['nginx:alpine', 'nginx:*-alpine'],
                DATA, [REPO + '/deploy/reactflow'], ['batch', 'reactflow'])

REAL = open(os.path.join(FIX, 'docker-compose.yml')).read()
MARK = "  inbox:\n    <<: *minion\n"
assert MARK in REAL

# «Чужое» на хосте: в chroot его нет, и compose его не прочитает.
os.makedirs('/tmp/otherapp', exist_ok=True)
open('/tmp/otherapp/compose.yml', 'w').write('services:\n  db:\n    image: nginx:alpine\n')
open('/tmp/otherapp/.env', 'w').write('DB_PASSWORD=supersecret\n')


def inbox(snippet):
    return REAL.replace(MARK, MARK + snippet, 1)


def render(text):
    shutil.rmtree(JAIL, ignore_errors=True)
    root = JAIL + REPO
    shutil.copytree(os.path.join(FIX, 'deploy'), root + '/deploy')
    open(root + '/.env', 'w').write('DRIVE_NAS=%s/\nTG_TOKEN_INBOX=x\n' % DATA)
    open(root + '/docker-compose.yml', 'w').write(text)
    shutil.copy(DC, JAIL + '/compose')
    p = subprocess.run(['/compose', '-p', 'minion2', '--project-directory', REPO,
                        '-f', REPO + '/docker-compose.yml', 'config', '--format', 'json'],
                       preexec_fn=lambda: (os.chroot(JAIL), os.chdir('/')), env={},
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if p.returncode:
        return None, 'рендер: ' + p.stderr.strip().splitlines()[-1][:100]
    return json.loads(p.stdout), ''


def real_ok(d):
    s = d['services']
    assert s['inbox']['user'] == USER and s['inbox']['environment']['HOME'] == '/tmp'
    assert 'user' not in s['batch'] and 'user' not in s['reactflow']
    assert all(NNP in x['security_opt'] for x in s.values())
    assert not [k for k in d if k.startswith('x-')]
    assert not [n for n, x in s.items() if 'env_file' in x]
    assert run_as_volumes(d, USER) == ['minion2_aggregator-state']


CASES = [
    ('настоящий compose', 'pass', REAL, real_ok),
    ('новый бот, подпапка данных', 'pass', REAL.replace(
        '\nvolumes:\n',
        '\n  newbot:\n    <<: *minion\n    command: python -m x\n'
        '    volumes:\n      - %s/bots/new:/data/new\n\nvolumes:\n' % DATA, 1), None),
    ('name: immich (перебивается -p)', 'pass', 'name: immich\n' + REAL, None),
    ('без no-new-privileges: добавим сами', 'pass', inbox('    security_opt: []\n'),
     lambda d: NNP in d['services']['inbox']['security_opt']),
    ('user: root у бота: перебьём', 'pass', inbox('    user: root\n'),
     lambda d: d['services']['inbox']['user'] == USER),
    ('bind / -> /host', 'block', inbox('    volumes:\n      - /:/host\n'), None),
    ('bind docker.sock', 'block', inbox('    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n'), None),
    ('bind рядом с данными', 'block', inbox('    volumes:\n      - /volume1/homes/user/Main:/m\n'), None),
    ('bind другой шары', 'block', inbox('    volumes:\n      - /volume1/homes:/homes\n'), None),
    ('bind через ../', 'block', inbox('    volumes:\n      - ${DRIVE_NAS}/../../../../docker:/d\n'), None),
    ('reactflow на запись', 'block', REAL.replace('./deploy/reactflow:/usr/share/nginx/html:ro', './deploy/reactflow:/usr/share/nginx/html'), None),
    ('privileged', 'block', inbox('    privileged: true\n'), None),
    ('cap_add SYS_ADMIN', 'block', inbox('    cap_add: [SYS_ADMIN]\n'), None),
    ('network_mode host', 'block', inbox('    network_mode: host\n'), None),
    ('pid host', 'block', inbox('    pid: host\n'), None),
    ('devices', 'block', inbox('    devices: ["/dev/sda:/dev/sda"]\n'), None),
    ('seccomp unconfined', 'block', inbox('    security_opt: ["seccomp:unconfined"]\n'), None),
    ('no-new-privileges:false', 'block', inbox('    security_opt: ["no-new-privileges:false"]\n'), None),
    ('чужой образ', 'block', inbox('    image: alpine:latest\n'), None),
    ('build на NAS', 'block', inbox('    build: .\n'), None),
    ('external том (чужая БД)', 'block', inbox('    volumes:\n      - pg:/x\n') + '  pg:\n    external: true\n    name: immich_pgdata\n', None),
    ('том с чужим именем', 'block', inbox('    volumes:\n      - pg:/x\n') + '  pg:\n    name: immich_pgdata\n', None),
    ('том driver_opts bind', 'block', inbox('    volumes:\n      - hv:/x\n') + '  hv:\n    driver_opts: {type: none, o: bind, device: /volume1}\n', None),
    ('external сеть', 'block', inbox('    networks: [imm]\n') + 'networks:\n  imm:\n    external: true\n    name: immich_default\n', None),
    ('env_file чужого приложения', 'block', inbox('    env_file: [.env, /tmp/otherapp/.env]\n'), None),
    ('env_file через \\u-escape', 'block', inbox('    "env\\u005ffile": /tmp/otherapp/.env\n'), None),
    ('secrets из файла хоста', 'block', inbox('    secrets: [s]\n') + 'secrets:\n  s:\n    file: /etc/shadow\n', None),
    ('include чужого compose', 'block', 'include:\n  - /tmp/otherapp/compose.yml\n' + REAL, None),
    ('extends чужого сервиса', 'block', REAL.replace(MARK, "  inbox:\n    extends: {file: /tmp/otherapp/compose.yml, service: db}\n    <<: *minion\n", 1), None),
    ('volumes_from', 'block', inbox('    volumes_from: [userbot]\n'), None),
    ('sysctls', 'block', inbox('    sysctls: {net.ipv4.ip_forward: 1}\n'), None),
    ('поддельная метка compose', 'block', inbox('    labels: {com.docker.compose.project: immich}\n'), None),
    ('bind propagation rshared', 'block', inbox('    volumes:\n      - {type: bind, source: "${DRIVE_NAS}", target: /data, bind: {propagation: rshared}}\n'), None),
    ('use_api_socket', 'block', inbox('    use_api_socket: true\n'), None),
    ('group_add administrators', 'block', inbox('    group_add: ["101"]\n'), None),
]

fails = 0
for title, expect, text, extra in CASES:
    cfg, why = render(text)
    if cfg is None:
        got = 'block'
    else:
        deploy, bad = policy.build_deploy(cfg, USER)
        got = 'block' if bad else 'pass'
        why = bad[0] if bad else 'ok'
        if got == 'pass' and extra:
            try:
                assert extra(deploy) is not False
            except AssertionError:
                got, why = 'FAILCHECK', 'проверка результата'
    ok = got == expect
    fails += not ok
    print('%s %-36s ждали=%-5s вышло=%-5s %s' % ('PASS' if ok else 'FAIL', title, expect, got, why[:90]))
shutil.rmtree(JAIL, ignore_errors=True)
print('\n%d/%d ok' % (len(CASES) - fails, len(CASES)))
sys.exit(1 if fails else 0)
