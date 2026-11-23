"""Общее для всех приложений: команды, журнал, остановки, состояние,
закреплённый docker compose, снапшоты, проверка здоровья, чистка образов.

Совместимо с Python 3.8 (системный python3 в DSM 7).
"""
import configparser
import hashlib
import json
import os
import posixpath
import re
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, 'state')
APPS = os.path.join(HERE, 'apps')

# Один compose для всего: рендер, деплой, откат. Скачивается один раз и
# сверяется по sha256 при каждом использовании.
COMPOSE_URL = ('https://github.com/docker/compose/releases/download/'
               'v2.29.7/docker-compose-linux-x86_64')
COMPOSE_SHA256 = '383ce6698cd5d5bbf958d2c8489ed75094e34a77d340404d9f32c4ae9e12baf0'
COMPOSE_BIN = os.path.join(HERE, 'bin', 'docker-compose')
REPEAT_MIN = 6 * 60

# Внешние программы; тесты подменяют их через окружение.
DOCKER = os.environ.get('DOCKER', 'docker')
GIT = os.environ.get('GIT', 'git')
BTRFS = os.environ.get('BTRFS', 'btrfs')
CURL = os.environ.get('CURL', 'curl')
SYNOACL = os.environ.get('SYNOACL', 'synoacltool')


class Stop(Exception):
    """Конец работы приложения с кодом выхода.

    kind=None: вердикт этой версии (app.key) — больше её не трогаем.
    kind='...': временный сбой этого вида — напомним через 6 ч.
    """

    def __init__(self, code, msg, kind=None):
        super().__init__(msg)
        self.code = code
        self.kind = kind


class Failed(Exception):
    """Внешняя команда завершилась с ошибкой."""


# ------------------------------------------------------------------ журнал
LOG = {'file': None, 'app': ''}


def log(msg, stamp=True):
    if stamp:
        app = '[%s] ' % LOG['app'] if LOG['app'] else ''
        line = '%s %s%s' % (time.strftime('%Y-%m-%d %H:%M:%S'), app, msg)
    else:
        line = msg
    print(line, flush=True)
    if LOG['file']:
        LOG['file'].write(line + '\n')
        LOG['file'].flush()


# ---------------------------------------------------------------- команды
def sh(*cmd, check=True, raw=False, merge=False, **kw):
    """Выполнить команду и вернуть stdout. Если упала (и check), Failed."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT if merge else subprocess.PIPE, **kw)
    if check and p.returncode:
        tail = (p.stderr or p.stdout or b'').decode('utf-8', 'replace').strip()
        raise Failed('%s: %s' % (' '.join(cmd[:3]), ' | '.join(tail.splitlines()[-4:])))
    return p.stdout if raw else p.stdout.decode('utf-8', 'replace')


def ok(*cmd):
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def http_get(url):
    return sh(CURL, '-fsSL', '--max-time', '60', '-H', 'User-Agent: nas-update', url)


def under(path, root):
    path, root = posixpath.normpath(path), posixpath.normpath(root)
    return path == root or path.startswith(root.rstrip('/') + '/')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, private=False):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
    if private:
        # На шаре с ACL chmod переводит файл в «Linux mode»: только root.
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def fresh(path, minutes):
    try:
        return time.time() - os.path.getmtime(path) < minutes * 60
    except OSError:
        return False


def code_sum(*extra):
    """Версия самого nas-update (и файлов extra): правка = отклонённое проверим заново."""
    h = hashlib.sha256()
    paths = [os.path.join(HERE, n) for n in sorted(os.listdir(HERE)) if n.endswith('.py')]
    for path in paths + list(extra):
        with open(path, 'rb') as f:
            h.update(f.read())
    return h.hexdigest()[:12]


# ----------------------------------------------------------------- compose
def compose_bin():
    if not (os.path.exists(COMPOSE_BIN) and sha256(COMPOSE_BIN) == COMPOSE_SHA256):
        log('скачиваю закреплённый docker compose: ' + COMPOSE_URL)
        os.makedirs(os.path.dirname(COMPOSE_BIN), exist_ok=True)
        tmp = COMPOSE_BIN + '.tmp'
        try:
            sh(CURL, '-fsSL', '--max-time', '600', '-o', tmp, COMPOSE_URL)
        except Failed as e:
            raise Stop(2, 'не скачал docker compose: %s' % e, 'network')
        if sha256(tmp) != COMPOSE_SHA256:
            os.remove(tmp)
            raise Stop(2, 'sha256 скачанного docker compose не совпал', 'network')
        os.chmod(tmp, 0o755)
        os.replace(tmp, COMPOSE_BIN)
    return COMPOSE_BIN


def compose(project, files, *args, project_dir=None, env=None, check=True):
    """docker compose проекта: всегда явные имя и файлы."""
    cmd = [os.environ.get('COMPOSE') or compose_bin(), '-p', project]
    if project_dir:
        cmd += ['--project-directory', project_dir]
    for f in files:
        cmd += ['-f', f]
    full_env = {'PATH': os.environ['PATH'], 'HOME': os.environ.get('HOME', '/root')}
    full_env.update(env or {})
    return sh(*(cmd + list(args)), check=check, merge=True, env=full_env)


# ----------------------------------------------------------------- docker
def inspect(*ids):
    """docker inspect тех, что есть (отсутствующих просто нет в ответе)."""
    if not ids:
        return []
    out = sh(DOCKER, 'inspect', *ids, check=False)
    try:
        return json.loads(out) if out.strip() else []
    except ValueError:
        return []


def project_containers(project):
    ids = sh(DOCKER, 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project).split()
    return inspect(*ids)


def restart_counts(project):
    return {c['Id']: c['RestartCount'] for c in project_containers(project)}


def sick_containers(cs, baseline=None):
    """Контейнеры, которые не running, не healthy (если есть healthcheck) или
    перезапускались сверх baseline — чистая проверка для wait_healthy и watch."""
    baseline = baseline or {}
    return [c for c in cs if c['State']['Status'] != 'running'
            or (c['State'].get('Health') or {}).get('Status', 'healthy') != 'healthy'
            or c['RestartCount'] > baseline.get(c['Id'], 0)]


def wait_healthy(project, settle, timeout, check=None, baseline=None):
    """Через settle секунд и дальше до timeout: все контейнеры проекта
    running, healthy (если есть healthcheck), без новых перезапусков и
    check() истинно. Иначе печатает, кто болен, и возвращает False."""
    baseline = baseline or {}
    log('жду %d с, потом проверяю контейнеры%s' % (
        settle, ' (до %d мин)' % (timeout // 60) if timeout else ''))
    time.sleep(settle)
    deadline = time.time() + timeout
    while True:
        cs = project_containers(project)
        sick = sick_containers(cs, baseline)
        good = bool(cs) and not sick and (check() if check else True)
        if good:
            log('все контейнеры живы')
            return True
        if time.time() >= deadline:
            break
        time.sleep(10)
    if not cs:
        log('у проекта нет контейнеров')
    for c in sick:
        log('  НЕЗДОРОВ: %s status=%s health=%s restarts=%d' % (
            c['Name'].lstrip('/'), c['State']['Status'],
            (c['State'].get('Health') or {}).get('Status', '-'), c['RestartCount']), stamp=False)
        for line in sh(DOCKER, 'logs', '--tail', '15', c['Id'], check=False, merge=True).splitlines():
            log('    | ' + line, stamp=False)
    return False


def remove_old_images(repos, keep_ids):
    """Все теги этих репозиториев образов, кроме запущенных образов."""
    keep = {k.split(':')[-1] for k in keep_ids}
    removed = 0
    for repo in repos:
        for line in sh(DOCKER, 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}} {{.ID}}',
                       repo).splitlines():
            ref, short = line.split()
            if any(k.startswith(short) for k in keep):
                continue
            if ok(DOCKER, 'rmi', short if ref.endswith(':<none>') else ref):
                removed += 1
    sh(DOCKER, 'image', 'prune', '-f', check=False)
    if removed:
        log('удалил старые образы: %d' % removed)


# --------------------------------------------------------------- снапшоты
def share_of(path):
    """/volume1/homes/... -> ('/volume1', 'homes')."""
    m = re.match(r'(.*?/volume\d+)/([^/]+)/', path.rstrip('/') + '/')
    if not m:
        raise Stop(2, '%s не на /volumeN/<шара>/...' % path, 'config')
    return m.groups()


def require_snapshot(path, max_age):
    """Свежий снапшот шары, где лежит path, или Stop."""
    volume, share = share_of(path)
    out = sh(BTRFS, 'subvolume', 'list', '-s', volume)
    pat = re.compile(r'\botime (\S+ \S+) .*\bpath .*@sharesnap/@?%s@?/' % re.escape(share))
    times = [m.group(1) for m in map(pat.search, out.splitlines()) if m]
    if not times:
        raise Stop(3, 'у шары %s нет снапшотов, без страховки не обновляю' % share, 'snapshot')
    when = max(times)
    age = (time.time() - time.mktime(time.strptime(when, '%Y-%m-%d %H:%M:%S'))) / 60
    log('снапшот %s: %s (%.0f мин назад)' % (share, when, age))
    if age > max_age:
        raise Stop(3, 'снапшот %s старше %d мин, без страховки не обновляю' % (share, max_age),
                   'snapshot')


# ------------------------------------------------------------- приложение
class App:
    """Одно приложение из apps/<имя>.conf. Источники обновлений — подклассы."""

    def __init__(self, name, section, dry):
        self.name = name
        self.dry = dry
        self.key = None  # версия, о которой будет вердикт
        self.state = os.path.join(STATE, name)
        os.makedirs(self.state, exist_ok=True)
        section = section or {}
        self.schedule = section.get('schedule', 'always').strip()
        self.snapshot_max_age = int(section.get('snapshot_max_age', 90))

    def path(self, name):
        return os.path.join(self.state, name)

    def salt(self):
        """Версия nas-update и конфига приложения: часть ключа вердикта."""
        return code_sum(os.path.join(APPS, self.name + '.conf'))

    def guarded_paths(self):
        """Пути, внутри которых не должен лежать сам nas-update."""
        return []

    # --- расписание: always или окно HH:MM-HH:MM раз в день
    def due(self):
        if self.dry or self.schedule == 'always':
            return True
        m = re.match(r'^(\d\d):(\d\d)-(\d\d):(\d\d)$', self.schedule)
        if not m:
            raise Stop(2, 'schedule: always или HH:MM-HH:MM, а не %r' % self.schedule, 'config')
        now = time.strftime('%H:%M')
        start, end = '%s:%s' % m.group(1, 2), '%s:%s' % m.group(3, 4)
        done_today = read_json(self.path('last_day.json')) == time.strftime('%Y-%m-%d')
        return start <= now < end and not done_today

    def mark_done(self):
        if not self.dry and self.schedule != 'always':
            write_json(self.path('last_day.json'), time.strftime('%Y-%m-%d'))

    # --- вердикты и письма
    def rejected(self):
        """Причина, если эту версию (self.key) уже отклонили."""
        v = read_json(self.path('verdict.json'), {})
        return v.get('reason') if v.get('key') == self.key else None

    def stopped(self, e):
        """Код выхода для Stop: письмо об одной проблеме приходит один раз."""
        log(('ОШИБКА: %s' if e.code == 2 else '%s') % e)
        if self.dry:
            return e.code
        if e.kind is None:
            if self.key:
                write_json(self.path('verdict.json'), {'key': self.key, 'reason': str(e)})
            return e.code
        marker = self.path('alert.' + e.kind)
        if fresh(marker, REPEAT_MIN):
            log('(уже сообщал, повторю не раньше чем через 6 ч)')
            return 0
        open(marker, 'w').close()
        return e.code

    def clear_alerts(self):
        for name in os.listdir(self.state):
            if name.startswith('alert.'):
                os.remove(self.path(name))

    def run(self):
        """Проверить и при необходимости обновить. Вернуть код (0 или 3)."""
        raise NotImplementedError


def load_conf(path):
    cp = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
    try:
        found = cp.read(path, encoding='utf-8')
    except configparser.Error as e:
        raise Stop(2, 'ошибка в %s: %s' % (path, e), 'config')
    if not found or 'app' not in cp:
        raise Stop(2, '%s: нужен раздел [app]' % path, 'config')
    return cp['app']
