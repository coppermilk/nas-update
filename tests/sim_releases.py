"""Симуляция NAS для source=releases (на примере Immich): стабы docker/compose/
curl/btrfs, состояние «работающей» версии — в файлах."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = '/tmp/nas-update-sim-rel'
ST = S + '/stub'
D = S + '/volume1/docker/immich'
GD = S + '/volume1/docker/nas-update'
CONF = GD + '/apps/immich.conf'
shutil.rmtree(S, ignore_errors=True)
PG = 'ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0@sha256:bcf63357'
VK = 'docker.io/valkey/valkey:9@sha256:4963247a'
FAILS = []


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def read(path):
    with open(path) as f:
        return f.read().strip()


def compose(pg=PG, vk=VK):
    return ('name: immich\nservices:\n  immich-server:\n    image: ghcr.io/immich-app/immich-server:${IMMICH_VERSION:-release}\n'
            '  immich-machine-learning:\n    image: ghcr.io/immich-app/immich-machine-learning:${IMMICH_VERSION:-release}\n'
            '  redis:\n    image: %s\n  database:\n    image: %s\n' % (vk, pg))


write(D + '/docker-compose.yml', compose())
write(D + '/docker-compose.override.yml', 'services: {}\n')
write(D + '/.env', 'UPLOAD_LOCATION=./library\nDB_PASSWORD=secret\nIMMICH_VERSION=v3\n')
write(D + '/library/backups/immich-db-backup-20261121T130000-v3.4.1-pg14.19.sql.gz', 'x')
for f in os.listdir(ROOT):
    if f.endswith('.py') or f == 'nas-update.sh':
        shutil.copy(os.path.join(ROOT, f), GD + '/' + f) if os.path.isdir(GD) else \
            (os.makedirs(GD), shutil.copy(os.path.join(ROOT, f), GD + '/' + f))
BASE_CONF = read(os.path.join(ROOT, 'examples', 'immich.conf')).replace(
    'dir     = /volume1/docker/immich', 'dir     = ' + D).replace(
    'schedule = 12:20-12:55', 'schedule = always').replace(
    'health_timeout = 900', 'health_timeout = 0\nsettle_sec = 0')
write(CONF, BASE_CONF)
write(ST + '/running', 'v3.4.1')
REL = []


def release(tag, days_ago, notes='bug fixes', pre=False, ref=None):
    t = datetime.datetime.utcnow() - datetime.timedelta(days=days_ago)
    REL.insert(0, {'tag_name': tag, 'published_at': t.strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'prerelease': pre, 'draft': False, 'body': notes})
    write(ST + '/releases.json', json.dumps(REL))
    write(ST + '/compose.' + tag, ref or compose(vk='docker.io/valkey/valkey:9@sha256:418652cf'))


def snapshot(minutes_ago=10):
    write(ST + '/snap', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - minutes_ago * 60)))


def stub(name, body):
    write(ST + '/' + name, '#!/bin/sh\nST=%s\n%s' % (ST, body), 0o755)


stub('docker', r'''echo "docker $*" >> $ST/calls
case "$1" in
ps) echo c-server; echo c-ml; echo c-db; echo c-redis; exit 0;;
inspect) shift; v=$(cat $ST/running); h=healthy; [ -f $ST/sick ] && h=unhealthy; sep=''; printf '['
    for id; do case "$id" in
      c-server) n=immich_server; img="ghcr.io/immich-app/immich-server:v3";;
      c-ml) n=immich_machine_learning; img="ghcr.io/immich-app/immich-machine-learning:v3";;
      c-db) n=immich_postgres; img="ghcr.io/immich-app/postgres:14";;
      c-redis) n=immich_redis; img="valkey:9";;
      *) continue;; esac
      printf '%s{"Id": "%s", "Name": "/%s", "Image": "sha256:%s%s", "RestartCount": 0, "Config": {"Image": "%s", "Env": ["IMMICH_SOURCE_REF=%s", "DB_PASSWORD=x"]}, "State": {"Status": "running", "Health": {"Status": "%s"}}}' \
        "$sep" "$id" "$n" "$id" "$v" "$img" "$v" "$h"; sep=,
    done; echo ']'; exit 0;;
image) case "$2" in
    ls) repo=$5; v=$(cat $ST/running); echo "$repo:v3 0ld000000001"; echo "$repo:v3.4.1 0ld000000002"
        case "$repo" in *server) echo "$repo:$v c-server$v";; *) echo "$repo:$v c-ml$v";; esac; exit 0;;
  esac; exit 0;;
logs) echo "stub: migration failed"; exit 0;;
esac
exit 0
''')
stub('compose', r'''echo "compose $* IMMICH_VERSION=$IMMICH_VERSION" >> $ST/calls
for a; do case "$a" in
  pull) [ -f $ST/pull_fail ] && exit 1; echo "$IMMICH_VERSION" >> $ST/pulled; exit 0;;
  up) echo "$IMMICH_VERSION" > $ST/running
      if grep -qx "$IMMICH_VERSION" $ST/bad 2>/dev/null; then touch $ST/sick; else rm -f $ST/sick; fi; exit 0;;
esac; done
exit 0
''')
stub('curl', r'''for u; do :; done
case "$u" in
*/releases\?*) cat $ST/releases.json;;
*/download/*/docker-compose.yml) t=${u%/docker-compose.yml}; t=${t##*/}; cat $ST/compose.$t;;
*/api/server/version) [ -f $ST/sick ] && exit 7
    v=$(cat $ST/running); v=${v#v}; IFS=. read a b c <<EOF
$v
EOF
    printf '{"major": %s, "minor": %s, "patch": %s}' $a $b $c;;
*) exit 22;;
esac
''')
stub('btrfs', 'echo "ID 1 gen 1 cgen 1 top level 5 otime $(cat $ST/snap) path @sharesnap/docker/GMT+01-x"\n')
ENV = dict(os.environ, DOCKER=ST + '/docker', COMPOSE=ST + '/compose', CURL=ST + '/curl', BTRFS=ST + '/btrfs',
           PYTHON=sys.executable)  # тот же python, что у теста
OUT = ''


def run(title, expect, *args):
    global OUT
    write(ST + '/calls', '')
    p = subprocess.run(['sh', GD + '/nas-update.sh'] + list(args), env=ENV,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    OUT = p.stdout
    ok = p.returncode == expect
    print('%s [%d] %s' % ('PASS' if ok else 'FAIL', p.returncode, title))
    if not ok:
        FAILS.append(title)
        print('\n'.join('      | ' + x for x in OUT.splitlines()[-15:]))


def check(title, cond):
    print('     %s: %s' % ('ok' if cond else 'НЕ OK', title))
    if not cond:
        FAILS.append(title)
        print('\n'.join('      | ' + x for x in OUT.splitlines()[-15:]))


def running():
    return read(ST + '/running')


def calls():
    return read(ST + '/calls')


def composed():
    """Был ли вызов compose (а не просто docker ps с меткой compose)."""
    return any(line.startswith('compose ') for line in calls().splitlines())


release('v3.4.0-rc.3', 16, 'may contain breaking changes', pre=True)
release('v3.4.0', 12)
release('v3.4.1', 11)
release('v3.4.2', 4)
release('v3.4.3', 1)
snapshot()

print('== A. dry-run')
run('dry-run', 0, '--dry-run')
check('цель v3.4.2, свежая v3.4.3 ждёт', 'цель: v3.4.2' in OUT and 'ждут 3 дн.: v3.4.3' in OUT
      and 'обновил бы v3.4.1 -> v3.4.2' in OUT)
check('сервер отвечает и дамп виден', 'сервер отвечает v3.4.1' in OUT and 'v3.4.1-pg14' in OUT)
check('ничего не трогал', not composed() and running() == 'v3.4.1')

print('== B. обновление')
run('v3.4.1 -> v3.4.2', 0)
check('прежние образы под точным тегом', 'tag sha256:c-serverv3.4.1 ghcr.io/immich-app/immich-server:v3.4.1' in calls())
check('pull и up именно v3.4.2', read(ST + '/pulled').split() == ['v3.4.2'] and running() == 'v3.4.2')
check('старые образы удалены, текущие нет', 'rmi ghcr.io/immich-app/immich-server:v3' in calls()
      and 'rmi ghcr.io/immich-app/immich-server:v3.4.1' in calls()
      and 'immich-server:v3.4.2' not in calls().split('rmi')[-1])
check('.env не тронут', 'IMMICH_VERSION=v3\n' in open(D + '/.env').read())
run('повтор', 0)
check('нечего', 'обновлять нечего' in OUT and not composed())

print('== C. в заметках breaking')
release('v3.4.4', 5, 'Breaking: removed env var')
run('breaking: не ставлю', 3)
check('причина и версия', '«breaking»' in OUT and running() == 'v3.4.2')
run('повтор: тихо', 0)
check('уже отложена', 'v3.4.4 уже отложена' in OUT)

print('== D. другой образ базы в эталонном compose')
release('v3.4.5', 5, ref=compose(pg='ghcr.io/immich-app/postgres:16-vectorchord0.5@sha256:new'))
run('новая база: не ставлю', 3)
check('причина', 'другой образ postgres' in OUT and running() == 'v3.4.2')

print('== E. новая версия не поднялась: откат')
release('v3.5.0', 4)
write(ST + '/bad', 'v3.5.0\n')
run('v3.5.0 падает: откат', 4)
check('лог и откат на v3.4.2', 'migration failed' in OUT and running() == 'v3.4.2' and 'вернул v3.4.2' in OUT)
check('старые образы не тронуты', ' rmi ' not in calls())
run('повтор: тихо', 0)

print('== F. нет свежего снапшота')
release('v3.5.1', 4)
snapshot(300)
run('старый снапшот: стоп', 3)
check('стек не тронут', not composed() and running() == 'v3.4.2')
run('снова: письмо уже было', 0)
check('тихо', 'уже сообщал' in OUT)
snapshot()
run('снапшот есть: обновил', 0)
check('v3.5.1', running() == 'v3.5.1')

print('== G. вышел v4')
release('v4.0.0', 2)
release('v3.5.2', 4)
run('v4: обновил в рамках v3 и написал про v4', 3)
check('v3.5.2 и подсказка', running() == 'v3.5.2' and 'IMMICH_VERSION=v4' in OUT)
run('про v4 второй раз не пишет', 0)

print('== H. окно расписания')
release('v3.5.3', 4)
now = datetime.datetime.now()
outside = (now + datetime.timedelta(hours=2)).strftime('%H:00') + '-' + (now + datetime.timedelta(hours=2)).strftime('%H:30')
write(CONF, BASE_CONF.replace('schedule = always', 'schedule = ' + outside))
run('вне окна: молчит', 0)
check('ничего не делал', OUT.strip() == '' and running() == 'v3.5.2')
inside = (now - datetime.timedelta(minutes=10)).strftime('%H:%M') + '-' + (now + datetime.timedelta(minutes=10)).strftime('%H:%M')
if inside[:5] < inside[6:]:  # не через полночь
    write(CONF, BASE_CONF.replace('schedule = always', 'schedule = ' + inside))
    run('в окне: обновил', 0)
    check('v3.5.3', running() == 'v3.5.3')
    run('в окне второй раз за день: молчит', 0)
    check('ничего не делал', OUT.strip() == '')
write(CONF, BASE_CONF)

print('== I. откат тоже не поднялся')
release('v3.5.4', 4)
write(ST + '/bad', 'v3.5.4\nv3.5.3\nv3.5.2\n')
run('оба не поднялись', 5)
check('подсказка про снапшот', 'Восстанови из снапшота' in OUT)
os.remove(ST + '/bad')

print('== J. настройки')
write(D + '/.env', 'IMMICH_VERSION=release\n')
run('IMMICH_VERSION=release: отказ', 2, '--dry-run')
check('причина', 'IMMICH_VERSION=vN' in OUT)

print()
print('ВСЕ СЦЕНАРИИ OK' if not FAILS else 'ПРОВАЛОВ: %d: %s' % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
