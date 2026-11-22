"""Симуляция NAS для source=git: настоящие git и рендер compose в chroot,
стабы docker/compose/btrfs/synoacltool.

Нужны root (chroot, смена владельца, fork от другого пользователя) и
закреплённый compose: COMPOSE_BIN=/путь/docker-compose. Пользователь run_as —
nobody.
"""
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = '/tmp/nas-update-sim-git'
ST = S + '/stub'
DATA = S + '/volume1/homes/user/Main/Drive'
REPO = S + '/volume1/docker/minion2'
GD = S + '/volume1/docker/nas-update'
STATE = GD + '/state/minion2'
VOL = S + '/docker/volumes'
GIT = ['git', '-c', 'user.email=a@b', '-c', 'user.name=agent']
FAILS = []


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, **kw).stdout


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def read(path, default=''):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


# ------------------------------------------------------------ окружение
shutil.rmtree(S, ignore_errors=True)
W = S + '/work'
sh('git', 'init', '-q', '--bare', S + '/origin')
sh('git', '-C', S + '/origin', 'symbolic-ref', 'HEAD', 'refs/heads/main')
shutil.copytree(os.path.join(ROOT, 'tests', 'fixtures', 'repo'), W)
sh('git', '-C', W, 'init', '-q')
sh('git', '-C', W, 'checkout', '-q', '-b', 'main')
sh('git', '-C', W, 'remote', 'add', 'origin', S + '/origin')


def push(msg):
    sh(*GIT, '-C', W, 'add', '-A')
    sh(*GIT, '-C', W, 'commit', '-qm', msg)
    sh('git', '-C', W, 'push', '-q', '-f', 'origin', 'main')
    return sh('git', '-C', W, 'rev-parse', 'HEAD').strip()


def publish(commit):
    """CI собрал образ из коммита: он появится в «реестре» при следующем pull."""
    write(ST + '/rev.' + commit[:12], commit)
    write(ST + '/next_id', 'sha256:' + commit[:12])


def edit(path, old, new):
    p = os.path.join(W, path)
    text = read(p)
    assert old in text, old
    write(p, text.replace(old, new, 1))


BASE = push('init')
sh('git', 'clone', '-q', S + '/origin', REPO)
write(REPO + '/.env', 'DRIVE_NAS=%s/\nTG_TOKEN_INBOX=secret\n' % DATA)

# данные: свои папки бота открыты, личная закрыта
for d, mode in (('bots', 0o777), ('bots/_data', 0o777), ('_inbox', 0o777), ('Personal', 0o700)):
    os.makedirs(os.path.join(DATA, d), exist_ok=True)
    os.chmod(os.path.join(DATA, d), mode)
write(DATA + '/bots/_data/state.json', '{}', 0o666)
write(DATA + '/Personal/diary.txt', 'secret', 0o600)
os.chmod(DATA, 0o755)
write(VOL + '/minion2_aggregator-state/_data/telethon.session', '')
write(VOL + '/minion2_n8n-data/_data/db.sqlite', '')

os.makedirs(GD + '/bin')
for f in os.listdir(ROOT):
    if f.endswith('.py') or f == 'nas-update.sh':
        shutil.copy(os.path.join(ROOT, f), GD)
shutil.copy(os.environ['COMPOSE_BIN'], GD + '/bin/docker-compose')
CONF = GD + '/apps/minion2.conf'
write(CONF, '''[app]
source = git
schedule = always
repo = %s
repo_url = %s
branch = main
project = minion2
image = ghcr.io/coppermilk/minion2:latest
other_images = nginx:alpine nginx:*-alpine
data = %s
readonly = ${repo}/deploy/reactflow
run_as = nobody
root_services = batch reactflow
protected = .env .env.local *.session *.session-journal
verify_sec = 0
''' % (REPO, S + '/origin', DATA))

# стабы
write(ST + '/git', '''#!/bin/sh
# protocol.file.allow=never -> always: origin в симуляции локальный
for x; do shift; [ "$x" = protocol.file.allow=never ] && x=protocol.file.allow=always; set -- "$@" "$x"; done
exec git "$@"
''', 0o755)
write(ST + '/docker', '''#!/bin/sh
ST=%(st)s
echo "docker $*" >> $ST/calls
case "$1" in
pull) [ -f $ST/pull_fail ] && exit 1; cp $ST/next_id $ST/image_id; exit 0;;
tag) case "$3" in nas-update/*) echo "$2" > $ST/deployed_id;; *) echo "$2" > $ST/image_id;; esac; exit 0;;
info) echo %(s)s/docker; exit 0;;
volume) printf '[{"Mountpoint": "%(vol)s/%%s/_data", "Labels": {"com.docker.compose.project": "minion2"}}]' "$3"; exit 0;;
ps) printf 'c1\\nc2\\n'; exit 0;;
image) case "$2" in ls) id=$(cat $ST/image_id); echo "$5:latest ${id#sha256:}"; echo "$5:<none> 0ld000000001";; esac; exit 0;;
inspect) shift; sep=''; printf '['
    for x; do case "$x" in
      ghcr.io/*) id=$(cat $ST/image_id); rev=$(cat $ST/rev.${id#sha256:} 2>/dev/null)
          printf '%%s{"Id": "%%s", "Config": {"Labels": {"org.opencontainers.image.revision": "%%s"}}}' "$sep" "$id" "$rev";;
      *) st=running; n=0
          if [ "$x" = c1 ] && [ -f $ST/sick ]; then st=restarting; n=3; rm -f $ST/sick; fi
          printf '%%s{"Id": "%%s", "Name": "/minion2-%%s-1", "State": {"Status": "%%s"}, "RestartCount": %%s, "Config": {}}' "$sep" "$x" "$x" "$st" "$n";;
    esac; sep=,; done; echo ']'; exit 0;;
logs) echo "PermissionError: [Errno 13] stub"; exit 0;;
esac
exit 0
''' % {'st': ST, 's': S, 'vol': VOL}, 0o755)
write(ST + '/compose', '''#!/bin/sh
ST=%(st)s
echo "compose $*" >> $ST/calls
for a; do case "$a" in
  up) n=$(cat $ST/up_fail 2>/dev/null || echo 0)
      if [ "$n" -gt 0 ]; then echo $((n-1)) > $ST/up_fail; echo "stub: up failed" >&2; exit 1; fi
      cp %(state)s/deploy.json $ST/last_up.json; exit 0;;
  create) touch %(vol)s/minion2_aggregator-state/_data/new.session; exit 0;;
esac; done
exit 0
''' % {'st': ST, 'state': STATE, 'vol': VOL}, 0o755)
write(ST + '/btrfs', '''#!/bin/sh
echo "ID 300 gen 9 cgen 9 top level 5 otime $(cat %(st)s/snap_time) path @sharesnap/homes/GMT+01-snap"
echo "ID 301 gen 9 cgen 9 top level 5 otime 2020-01-01 00:00:00 path @sharesnap/docker/GMT+01-old"
''' % {'st': ST}, 0o755)
write(ST + '/synoacltool', '''#!/bin/sh
echo "synoacltool $*" >> %(st)s/acl_calls
case "$1" in
  -get) case "$2" in */bots|*/_inbox) echo " [0] user:nobody:allow:rwxpdDaARWc--:fd-- (level:0)";;
        *) echo "(synoacltool.c, 596)It's Linux mode";; esac;;
  -enforce-inherit) chmod 666 "$2";;
esac
''' % {'st': ST}, 0o755)


def snapshot(minutes_ago=5):
    write(ST + '/snap_time', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - minutes_ago * 60)))


snapshot()
publish(BASE)
write(ST + '/image_id', 'sha256:' + BASE[:12])
ENV = dict(os.environ, DOCKER=ST + '/docker', COMPOSE=ST + '/compose', GIT=ST + '/git',
           BTRFS=ST + '/btrfs', SYNOACL=ST + '/synoacltool', PYTHON=sys.executable)  # тот же python, что у теста
OUT = ''


def run(title, expect, *args):
    global OUT
    for f in ('calls', 'acl_calls'):
        write(ST + '/' + f, '')
    p = subprocess.run(['sh', GD + '/nas-update.sh'] + list(args), env=ENV,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    OUT = p.stdout
    ok = p.returncode == expect
    print('%s [%d] %s' % ('PASS' if ok else 'FAIL', p.returncode, title))
    if not ok:
        FAILS.append(title)
        print('\n'.join('      | ' + line for line in OUT.splitlines()[-25:]))


def check(title, cond):
    print('     %s: %s' % ('ok' if cond else 'НЕ OK', title))
    if not cond:
        FAILS.append(title)
        print('\n'.join('      | ' + line for line in OUT.splitlines()[-25:]))


def calls():
    return read(ST + '/calls')


def composed():
    """Был ли вызов compose (а не просто docker ps с меткой compose)."""
    return any(line.startswith('compose ') for line in calls().splitlines())


def head():
    return sh('git', '-C', REPO, 'rev-parse', 'HEAD').strip()


def state(name):
    return json.loads(read(STATE + '/' + name, 'null'))


# ------------------------------------------------------------ сценарии
print('== A. dry-run')
run('dry-run на текущем коммите', 0, '--dry-run')
check('кто от кого', 'от nobody:' in OUT and ' inbox' in OUT and 'под root: batch reactflow' in OUT)
check('доступ проверен, личное не видно', 'пишет в _inbox, bots' in OUT and 'Personal' not in OUT)
check('ничего не записал', not os.path.exists(STATE + '/current.json') and not composed())

print('== B. первый запуск: принять текущее, стек не трогать')
run('первый запуск', 0)
check('принял HEAD', 'первый запуск' in OUT and state('current.json')['commit'] == BASE)
check('стек не трогал', not composed())
check('deploy.json только для root', oct(os.stat(STATE + '/deploy.json').st_mode & 0o777) == '0o600')
run('повтор', 0)
check('изменений нет', 'изменений нет' in OUT and not composed())

print('== C. агент: обычная правка + образ из CI')
edit('README.md', '\n', '\n\nчуть-чуть документации\n')
C = push('docs')
run('коммит есть, образа ещё нет', 0)
check('ждёт образ', 'ещё не опубликован' in OUT and head() == BASE)
publish(C)
run('образ появился: деплой', 0)
check('down/create/up', all(x in calls() for x in ('compose -p minion2', ' down ', ' create ', ' up -d')))
check('запущен проверенный JSON с user', state('deploy.json')['services']['inbox']['user'] == '65534:65534'
      and json.loads(read(ST + '/last_up.json')) == state('deploy.json'))
check('чекаут и запись обновлены', head() == C and state('current.json')['commit'] == C)
check('образ закреплён для отката', read(ST + '/deployed_id').strip() == 'sha256:' + C[:12])
check('старые образы убраны, текущий оставлен', 'rmi 0ld000000001' in calls() and ':latest' not in calls().split('rmi')[-1])
vol = VOL + '/minion2_aggregator-state/_data/'
check('том userbot отдан nobody', os.stat(vol + 'new.session').st_uid == 65534 == os.stat(vol + 'telethon.session').st_uid)
check('старый том n8n не тронут', os.stat(VOL + '/minion2_n8n-data/_data/db.sqlite').st_uid == 0)
check('у всех no-new-privileges', all('no-new-privileges:true' in s['security_opt']
                                      for s in state('deploy.json')['services'].values()))
GOOD = C


def evil(title, change, expect_in):
    change()
    c = push(title)
    publish(c)
    run(title, 3)
    check('причина: ' + expect_in, expect_in in OUT)
    check('стек и чекаут не тронуты', not composed() and head() == GOOD)
    run(title + ': повтор', 0)
    check('тихо', 'уже отклонил' in OUT)
    sh('git', '-C', W, 'reset', '-q', '--hard', GOOD)
    sh('git', '-C', W, 'clean', '-qfd')
    sh('git', '-C', W, 'push', '-q', '-f', 'origin', 'main')


print('== D. злые коммиты')
evil('bind / в контейнер', lambda: edit('docker-compose.yml', '  inbox:\n    <<: *minion\n',
                                         '  inbox:\n    <<: *minion\n    volumes:\n      - /:/host\n'), 'монтирует папку хоста /')
evil('env_file чужого приложения', lambda: edit('docker-compose.yml', '  inbox:\n    <<: *minion\n',
                                                '  inbox:\n    <<: *minion\n    env_file: /volume1/docker/immich/.env\n'), 'не рендерится')
evil('.env в репо', lambda: (write(W + '/.env', 'X=1\n'), sh('git', '-C', W, 'add', '-f', '.env')), 'защищённый файл .env')
evil('симлинк в репо', lambda: os.symlink('/volume1', W + '/deploy/evil'), 'симлинк в репо')
write(REPO + '/notes.txt', 'local')
evil('затёр бы локальный файл', lambda: write(W + '/notes.txt', 'agent'), 'затёр бы локальный файл notes.txt')
os.remove(REPO + '/notes.txt')
evil('privileged', lambda: edit('docker-compose.yml', '  inbox:\n    <<: *minion\n',
                                '  inbox:\n    <<: *minion\n    privileged: true\n'), 'privileged')

print('== E. нет свежего снапшота')
edit('README.md', 'чуть-чуть', 'ещё')
E = push('docs 2')
publish(E)
snapshot(200)
run('снапшот старый: стоп', 3)
check('стек не тронут', ' up ' not in calls() and head() == GOOD)
run('снова: письмо уже было', 0)
check('тихо', 'уже сообщал' in OUT)
snapshot()
run('снапшот появился: деплой', 0)
check('выкатил', head() == E and 'готово' in OUT)
check('метки тревоги сняты', not [f for f in os.listdir(STATE) if f.startswith('alert.')])
GOOD = E

print('== F. новая версия падает: откат')
edit('README.md', 'ещё', 'снова')
F = push('docs 3')
publish(F)
write(ST + '/sick', '')
run('падает: откат', 4)
check('в выводе причина и лог', 'НЕЗДОРОВ: minion2-c1-1' in OUT and 'PermissionError' in OUT)
check('вернул коммит, образ и JSON', head() == GOOD and read(ST + '/image_id').strip() == 'sha256:' + GOOD[:12]
      and state('current.json')['commit'] == GOOD)
run('та же версия: не трогаем', 0)
check('тихо', 'уже отклонил' in OUT and not composed())
with open(CONF, 'a') as f:
    f.write('# поправили конфиг\n')
run('поменяли конфиг: одна новая попытка', 0)
check('выкатил', head() == F and 'готово' in OUT)
GOOD = F

print('== G. up не проходит дважды: откат')
edit('README.md', 'снова', 'опять')
G = push('docs 4')
publish(G)
write(ST + '/up_fail', '2')
run('up упал: откат', 4)
check('работает прежняя', head() == GOOD)

print('== H. права: чинит файлы root в Linux mode, чужие не трогает')
write(DATA + '/bots/_data/blob', 'x', 0o711)            # создал контейнер от root
write(DATA + '/_inbox/user.txt', 'x', 0o600)
os.chown(DATA + '/_inbox/user.txt', 1000, 1000)          # файл человека
edit('README.md', 'опять', 'и ещё')
H = push('docs 5')
publish(H)
run('деплой с проверкой доступа', 0)
check('вернул наследование только файлу root', 'вернул наследование ACL у 1' in OUT
      and 'enforce-inherit %s/bots/_data/blob' % DATA in read(ST + '/acl_calls')
      and 'user.txt' not in read(ST + '/acl_calls').split('-enforce-inherit')[-1])
check('чужой файл показан, деплой прошёл', '_inbox/user.txt' in OUT and head() == H)
GOOD = H

print('== I. первое переключение на run_as с недоступными файлами: не переключает')
dep = state('deploy.json')
for s in dep['services'].values():
    s.pop('user', None)
write(STATE + '/deploy.json', json.dumps(dep))           # «сейчас всё под root»
edit('README.md', 'и ещё', 'и ещё раз')
I = push('docs 6')
publish(I)
run('недоступный файл человека: не переключаю', 3)
check('причина и стек не тронут', 'не переключаю' in OUT and ' up ' not in calls() and head() == GOOD)
os.remove(DATA + '/_inbox/user.txt')
with open(CONF, 'a') as f:
    f.write('# убрали файл\n')
run('убрали: переключил', 0)
check('выкатил', head() == I)

print('== J. настройки и место')
conf_ok = read(CONF)
write(CONF, conf_ok.replace('run_as = nobody', 'run_as = root'))
run('run_as = root: отказ', 2)
check('причина', 'это root' in OUT)
write(CONF, conf_ok.replace('data = %s' % DATA, 'data = %s/volume1/docker' % S))
run('nas-update внутри data: отказ', 2, '--dry-run')
check('причина', 'nas-update лежит внутри' in OUT)
write(CONF, conf_ok)

print()
print('ВСЕ СЦЕНАРИИ OK' if not FAILS else 'ПРОВАЛОВ: %d: %s' % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
