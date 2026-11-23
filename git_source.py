"""source = git: приложение из репозитория, куда пушат агенты (недоверенный код).

Каждый запуск:
  1. git fetch; ждём образ из реестра, собранный из этого же коммита
     (метка org.opencontainers.image.revision).
  2. Дерево коммита: только обычные файлы, без .env и сессий; git не
     затрёт локальные неотслеживаемые файлы.
  3. compose рендерится в chroot, где есть только этот коммит и .env:
     прочитать что-то ещё с хоста он не может физически.
  4. Рендер проходит политику, в него дописываются user (run_as) и
     no-new-privileges, и деплоится ровно этот JSON.
  5. Свежий снапшот шары с данными; run_as должен видеть свои папки.
  6. down/up; контейнеры должны прожить verify_sec без перезапусков,
     иначе откат на прежние коммит, образ и JSON.
"""
import fnmatch
import grp
import io
import json
import os
import posixpath
import pwd
import shutil
import stat
import subprocess
import sys
import tarfile

from core import (DOCKER, GIT, SYNOACL, App, Failed, Stop, compose, compose_bin,
                  inspect, log, project_containers, read_json, remove_old_images,
                  require_snapshot, restart_counts, sh, sick_containers, under,
                  wait_healthy, write_json)

# ---------------------------------------------------------------- политика
SERVICE_OK = set('''
    image command entrypoint environment healthcheck init restart pull_policy
    security_opt volumes ports expose networks depends_on container_name labels
    logging user working_dir tty stdin_open stop_grace_period stop_signal tmpfs
    ulimits hostname domainname extra_hosts dns dns_search dns_opt profiles
    deploy read_only shm_size oom_score_adj oom_kill_disable pids_limit scale
    annotations attach links mac_address mem_limit memswap_limit mem_reservation
    mem_swappiness cpus cpu_shares cpu_quota cpu_period cpuset cpu_count
    cpu_percent blkio_config cap_drop platform develop env_file
'''.split())

SERVICE_DENY = {
    'privileged': 'полный доступ к хосту',
    'cap_add': 'лишние права ядра',
    'devices': 'устройства хоста',
    'device_cgroup_rules': 'устройства хоста',
    'gpus': 'GPU хоста',
    'volumes_from': 'чужие тома',
    'external_links': 'чужие контейнеры',
    'network_mode': 'host/чужая сеть; можно только сети проекта',
    'pid': 'общий PID-namespace',
    'ipc': 'общий IPC',
    'uts': 'uts: host',
    'userns_mode': 'userns_mode',
    'cgroup': 'cgroup: host',
    'cgroup_parent': 'cgroup_parent',
    'sysctls': 'параметры ядра',
    'runtime': 'нестандартный runtime',
    'isolation': 'isolation',
    'storage_opt': 'storage_opt',
    'credential_spec': 'credential_spec',
    'group_add': 'группы хоста (administrators = личные файлы)',
    'build': 'на NAS ничего не собираем, только pull',
    'secrets': 'файлы хоста',
    'configs': 'файлы хоста',
    'label_file': 'файл хоста',
    'use_api_socket': 'Docker API = root',
    'provider': 'программы на хосте',
    'models': 'models',
}

WEAK_SECURITY_OPT = ('seccomp', 'apparmor', 'label:disable', 'systempaths')
NNP = 'no-new-privileges:true'


class Policy:
    """Что приложению можно: проект, образы, папки хоста, кто от root."""

    def __init__(self, project, image, other_images, data, readonly, root_services):
        self.project = project
        self.image = image
        self.image_repo = image.rsplit(':', 1)[0]
        self.other_images = other_images
        self.data = data.rstrip('/')
        self.readonly = readonly
        self.root_services = set(root_services)

    def main_image(self, image):
        return image == self.image_repo or image.startswith(self.image_repo + ':')

    def check_mount(self, svc, v, volumes):
        """Нарушение для одного монтирования или None."""
        kind = v.get('type')
        if kind == 'bind':
            src = v.get('source') or ''
            if not src.startswith('/'):
                return '%s: bind %r не абсолютный' % (svc, src)
            if (v.get('bind') or {}).get('propagation') not in (None, 'rprivate', 'private'):
                return '%s: bind propagation у %s' % (svc, src)
            if under(src, self.data):
                return None
            if any(under(src, r) for r in self.readonly):
                return None if v.get('read_only') else '%s: %s можно только на чтение' % (svc, src)
            return '%s: монтирует папку хоста %s' % (svc, src)
        if kind == 'volume':
            src = v.get('source')
            if src is not None and src not in volumes:
                return '%s: том %r не объявлен в проекте' % (svc, src)
            return None
        if kind == 'tmpfs':
            return None
        return '%s: тип монтирования %r' % (svc, kind)

    def build_deploy(self, cfg, user):
        """Рендер compose -> (JSON для деплоя, список нарушений).

        Проверяет, что контейнерам достаётся только своё, и приводит конфиг
        к тому, что запустим: user для главного образа, no-new-privileges
        всем, без x-полей.
        """
        bad = []
        if cfg.get('name') != self.project:
            bad.append('имя проекта %r вместо %r' % (cfg.get('name'), self.project))
        for k, v in cfg.items():
            if not (k.startswith('x-') or k in ('name', 'services', 'networks', 'volumes')
                    or (k in ('secrets', 'configs') and not v)):
                bad.append('верхний ключ %r' % k)

        volumes = cfg.get('volumes') or {}
        networks = cfg.get('networks') or {}
        for what, items, driver in (('том', volumes, 'local'), ('сеть', networks, 'bridge')):
            for key, item in items.items():
                item = item or {}
                if item.get('external'):
                    bad.append('%s %r external (чужой)' % (what, key))
                if item.get('driver') not in (None, '', driver):
                    bad.append('%s %r: драйвер %r' % (what, key, item.get('driver')))
                if item.get('driver_opts'):
                    bad.append('%s %r: driver_opts' % (what, key))
                if item.get('name') not in (None, '%s_%s' % (self.project, key)):
                    bad.append('%s %r называется %r (чужое имя)' % (what, key, item.get('name')))

        services = cfg.get('services') or {}
        if not services:
            bad.append('в compose нет сервисов')
        for name, s in services.items():
            for k in s:
                if k in SERVICE_DENY:
                    if s[k] not in (None, False, '', [], {}):
                        bad.append('%s: %s (%s)' % (name, k, SERVICE_DENY[k]))
                elif k not in SERVICE_OK and not k.startswith('x-'):
                    bad.append('%s: неизвестный ключ %r (проверь и добавь в git_source.py)' % (name, k))
            image = s.get('image') or ''
            if not (self.main_image(image) or any(fnmatch.fnmatchcase(image, p) for p in self.other_images)):
                bad.append('%s: образ %r не разрешён' % (name, image))
            if s.get('pull_policy') == 'build':
                bad.append('%s: pull_policy: build' % name)
            for opt in s.get('security_opt') or []:
                o = str(opt).lower().replace('=', ':')
                if o.startswith(WEAK_SECURITY_OPT) or 'unconfined' in o or o == 'no-new-privileges:false':
                    bad.append('%s: security_opt %r ослабляет изоляцию' % (name, opt))
            for label in s.get('labels') or {}:
                if label.startswith('com.docker.compose.'):
                    bad.append('%s: служебная метка %s' % (name, label))
            if (((s.get('deploy') or {}).get('resources') or {}).get('reservations') or {}).get('devices'):
                bad.append('%s: deploy...devices' % name)
            for net in s.get('networks') or {}:
                if net not in networks:
                    bad.append('%s: сеть %r не объявлена в проекте' % (name, net))
            for v in s.get('volumes') or []:
                err = self.check_mount(name, v, volumes)
                if err:
                    bad.append(err)

        deploy = {k: v for k, v in cfg.items() if k in ('name', 'services', 'networks', 'volumes')}
        for name, s in services.items():
            s.pop('env_file', None)  # уже влит в environment при рендере
            sec = s.setdefault('security_opt', [])
            if NNP not in sec:
                sec.append(NNP)
            if self.main_image(s.get('image') or '') and name not in self.root_services:
                s['user'] = user
                env = s.get('environment') or {}
                env.update(HOME='/tmp', XDG_CACHE_HOME='/tmp/.cache')  # у uid нет домашней папки
                s['environment'] = env
        return deploy, bad


def run_as_services(deploy, user):
    return sorted(n for n, s in deploy['services'].items() if s.get('user') == user)


def run_as_volumes(deploy, user):
    """Полные имена томов, которые монтируют сервисы от user."""
    names = set()
    for s in deploy['services'].values():
        if s.get('user') != user:
            continue
        for v in s.get('volumes') or []:
            if v.get('type') == 'volume' and v.get('source'):
                names.add(deploy['volumes'][v['source']].get('name')
                          or '%s_%s' % (deploy['name'], v['source']))
    return sorted(names)


# ------------------------------------------------- доступ run_as к данным
SKIP_NAMES = {'@eaDir', '#recycle', '#snapshot'}


def scan_access(data):
    """От имени проверяемого пользователя: папки верхнего уровня, куда он
    может писать, и всё внутри них, к чему у него нет полного доступа."""
    tops, bad, total = [], [], 0
    for n in sorted(os.listdir(data)):
        p = os.path.join(data, n)
        if n[:1] not in '@#' and os.path.isdir(p) and not os.path.islink(p) \
                and os.access(p, os.R_OK | os.W_OK | os.X_OK):
            tops.append(n)
    for top in tops:
        for dirpath, dirs, files in os.walk(os.path.join(data, top)):
            keep = []
            for d in dirs:
                p = os.path.join(dirpath, d)
                if d in SKIP_NAMES or os.path.islink(p):
                    continue
                total += 1
                if not os.access(p, os.R_OK | os.W_OK | os.X_OK):
                    bad.append(p)
                if os.access(p, os.R_OK | os.X_OK):
                    keep.append(d)
            dirs[:] = keep
            for f in files:
                p = os.path.join(dirpath, f)
                if f in SKIP_NAMES or os.path.islink(p):
                    continue
                total += 1
                if not os.access(p, os.R_OK | os.W_OK):
                    bad.append(p)
    return {'tops': tops, 'total': total,
            'bad': [os.path.relpath(p, data) for p in bad]}


def as_user(pw, fn, *args):
    """fn(*args) в дочернем процессе с правами pw; результат через JSON."""
    sys.stdout.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.setgroups(os.getgrouplist(pw.pw_name, pw.pw_gid))
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)
            os.chdir('/')
            with os.fdopen(w, 'w', encoding='utf-8', errors='surrogateescape') as f:
                json.dump(fn(*args), f)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(w)
    with os.fdopen(r, encoding='utf-8', errors='surrogateescape') as f:
        out = f.read()
    _, status = os.waitpid(pid, 0)
    if status:
        raise Failed('проверка доступа от %s не выполнилась' % pw.pw_name)
    return json.loads(out)


# --------------------------------------------------------------- источник
class GitApp(App):
    def __init__(self, name, c, dry):
        super().__init__(name, c, dry)
        try:
            self.repo = c['repo']
            self.repo_url = c['repo_url']
            self.branch = c.get('branch', 'main')
            self.project = c['project']
            self.image = c['image']
            self.run_as = c['run_as']
            self.protected = c.get('protected', '.env').split()
            self.verify_sec = c.getint('verify_sec', 60)
            self.policy = Policy(
                self.project, self.image, c.get('other_images', '').split(), c['data'],
                [p.strip() for p in c.get('readonly', '').splitlines() if p.strip()],
                c.get('root_services', '').split())
        except (KeyError, ValueError) as e:
            raise Stop(2, '%s.conf: %s' % (name, e), 'config')
        self.deploy_file = self.path('deploy.json')
        self.current_file = self.path('current.json')
        self.deployed_tag = 'nas-update/%s:deployed' % name

    def guarded_paths(self):
        return [self.repo, self.policy.data] + self.policy.readonly

    def version_info(self):
        """Что стоит сейчас — для отчёта о версии на сайт (report.py)."""
        cur = self.current() or {}
        return {'commit': cur.get('commit'), 'image': cur.get('image'), 'branch': self.branch}

    def check_user(self):
        try:
            self.pw = pwd.getpwnam(self.run_as)
        except KeyError:
            raise Stop(2, 'в DSM нет пользователя %s (run_as)' % self.run_as, 'config')
        self.user = '%d:%d' % (self.pw.pw_uid, self.pw.pw_gid)
        if self.pw.pw_uid == 0 or self.pw.pw_gid == 0:
            raise Stop(2, 'run_as=%s: это root' % self.run_as, 'config')
        try:
            admins = grp.getgrnam('administrators').gr_gid
        except KeyError:
            admins = None
        if admins in os.getgrouplist(self.run_as, self.pw.pw_gid):
            raise Stop(2, '%s в группе administrators: убери его оттуда' % self.run_as, 'config')

    def version_key(self, commit, image_id):
        return '%s %s %s' % (commit, image_id, self.salt())

    # --- git (без хуков и fsmonitor: из .git/config root ничего не запустит)
    def git(self, *args, **kw):
        return sh(GIT, '-C', self.repo, '-c', 'core.hooksPath=/dev/null',
                  '-c', 'core.fsmonitor=false', '-c', 'submodule.recurse=false',
                  '-c', 'protocol.file.allow=never', *args, **kw)

    def fetch(self):
        if not os.path.isdir(os.path.join(self.repo, '.git')):
            raise Stop(2, 'в %s нет git-чекаута' % self.repo, 'config')
        b = self.branch
        try:
            self.git('remote', 'set-url', 'origin', self.repo_url)
            self.git('fetch', '-q', 'origin', '+refs/heads/%s:refs/remotes/origin/%s' % (b, b))
        except Failed as e:
            raise Stop(2, 'git fetch не прошёл (сеть?): %s' % e, 'network')
        return self.git('rev-parse', 'refs/remotes/origin/' + b).strip()

    def describe(self, commit):
        return '%s (%s)' % (commit[:12], self.git('log', '-1', '--format=%ci %s', commit).strip())

    def ls_tree(self, ref):
        entries = {}
        for rec in self.git('ls-tree', '-r', '-z', '--full-tree', ref).split('\0'):
            if rec:
                meta, path = rec.split('\t', 1)
                entries[path] = meta.split()[0]
        return entries

    def check_tree(self, commit):
        """Только обычные файлы; ни .env, ни сессий; git не затрёт неотслеживаемое."""
        bad = []
        tracked = self.ls_tree('HEAD')
        for path, mode in self.ls_tree(commit).items():
            if mode == '120000':
                bad.append('симлинк в репо: ' + path)
            elif mode == '160000':
                bad.append('git-сабмодуль: ' + path)
            elif mode not in ('100644', '100755'):
                bad.append('режим %s у %s' % (mode, path))
            name = posixpath.basename(path)
            if any(fnmatch.fnmatchcase(path if '/' in p else name, p) for p in self.protected):
                bad.append('в репо закоммичен защищённый файл ' + path)
            if path not in tracked and os.path.lexists(os.path.join(self.repo, path)):
                bad.append('git затёр бы локальный файл ' + path)
        return bad

    # --- рендер в chroot
    def render(self, commit):
        """`compose config` в chroot, где есть только этот коммит, .env и сам compose."""
        jail = self.path('jail')
        shutil.rmtree(jail, ignore_errors=True)
        root = jail + self.repo
        os.makedirs(root)
        try:
            with tarfile.open(fileobj=io.BytesIO(self.git('archive', commit, raw=True))) as t:
                for m in t:
                    if not (m.isfile() or m.isdir()) or m.name.startswith('/') \
                            or '..' in m.name.split('/'):
                        raise Stop(3, 'странный элемент в архиве коммита: %s' % m.name)
                t.extractall(root, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
            env = os.path.join(self.repo, '.env')
            if os.path.isfile(env):
                shutil.copyfile(env, os.path.join(root, '.env'))
            os.link(compose_bin(), os.path.join(jail, 'compose'))
            p = subprocess.run(
                ['/compose', '-p', self.project, '--project-directory', self.repo,
                 '-f', self.repo + '/docker-compose.yml', 'config', '--format', 'json'],
                preexec_fn=lambda: (os.chroot(jail), os.chdir('/')), env={},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            shutil.rmtree(jail, ignore_errors=True)  # там копия .env
        if p.returncode:
            err = p.stderr.decode('utf-8', 'replace').strip().splitlines()
            raise Stop(3, 'compose не рендерится: %s' % (err[-1] if err else p.returncode))
        return json.loads(p.stdout)

    def prepare(self, commit):
        """Коммит -> проверенный JSON для деплоя (или Stop 3)."""
        bad = self.check_tree(commit)
        if not bad:
            deploy, bad = self.policy.build_deploy(self.render(commit), self.user)
            for s in deploy['services'].values():
                for v in s.get('volumes') or []:
                    src = v.get('source') or ''
                    if v.get('type') == 'bind' and os.path.realpath(src) != posixpath.normpath(src):
                        bad.append('путь %s идёт через симлинк' % src)
        if bad:
            for b in bad:
                log('  ✗ ' + b, stamp=False)
            raise Stop(3, 'заблокировано: %s нарушает политику' % commit[:12])
        names = run_as_services(deploy, self.user)
        log('compose прошёл проверку: %d сервисов; от %s: %s; под root: %s' % (
            len(deploy['services']), self.run_as, ' '.join(names),
            ' '.join(sorted(set(deploy['services']) - set(names))) or '-'))
        return deploy

    # --- docker
    def image_info(self, ref):
        """(id, revision) локального образа или (None, None)."""
        found = [i for i in inspect(ref) if 'Config' in i]
        if not found:
            return None, None
        labels = (found[0].get('Config') or {}).get('Labels') or {}
        return found[0]['Id'], labels.get('org.opencontainers.image.revision')

    def dc(self, *args):
        return compose(self.project, [self.deploy_file], *args)

    def chown_volumes(self, deploy):
        """Тома сервисов run_as Docker создаёт от root: отдаём их run_as."""
        root = sh(DOCKER, 'info', '-f', '{{.DockerRootDir}}').strip() + '/volumes/'
        for name in run_as_volumes(deploy, self.user):
            info = json.loads(sh(DOCKER, 'volume', 'inspect', name))[0]
            mp = info['Mountpoint']
            if (info.get('Labels') or {}).get('com.docker.compose.project') != self.project \
                    or not mp.startswith(root):
                log('том %s не из проекта %s, не трогаю' % (name, self.project))
                continue
            n = 0
            for dirpath, dirs, files in os.walk(mp):
                for p in [dirpath] + [os.path.join(dirpath, x) for x in dirs + files]:
                    st = os.lstat(p)
                    if (st.st_uid, st.st_gid) != (self.pw.pw_uid, self.pw.pw_gid):
                        os.lchown(p, self.pw.pw_uid, self.pw.pw_gid)
                        n += 1
            if n:
                log('том %s: отдал %s %d объект(ов)' % (name, self.run_as, n))

    def apply(self, commit, image_id, deploy):
        """Перевести стек на (коммит, образ, JSON). False: не поднялся."""
        try:
            self.git('reset', '-q', '--hard', commit)  # чекаут нужен для readonly-папок
            sh(DOCKER, 'tag', image_id, self.image)
            write_json(self.deploy_file, deploy, private=True)
            self.dc('down', '--remove-orphans', '--timeout', '30')
            self.dc('create', '--remove-orphans')
            self.chown_volumes(deploy)
            try:
                self.dc('up', '-d', '--remove-orphans')
            except Failed:
                self.dc('up', '-d', '--remove-orphans')
        except Failed as e:
            log(str(e))
            return False
        return wait_healthy(self.project, self.verify_sec, 0)

    # --- доступ run_as к данным
    def fix_access(self, bad, plan=False):
        """Вернуть наследование ACL тому, что закрыто от run_as, но только
        если это безопасно: владелец root (файл создал контейнер), «Linux
        mode» (chmod в контейнере снял ACL DSM), внутри папки, где запись
        run_as выдана в DSM явно, не симлинк и без выхода из папки."""
        data = self.policy.data
        granted = {}
        fixed = skipped = 0
        for rel in sorted(bad):  # папка раньше своего содержимого
            parts = rel.split('/')
            top, p = parts[0], os.path.join(data, rel)
            if top not in granted:
                acl = sh(SYNOACL, '-get', os.path.join(data, top), check=False)
                granted[top] = 'user:%s:allow:rw' % self.run_as in acl
            try:
                st = os.lstat(p)
            except OSError:
                st = None
            if '..' in parts or not granted[top] or st is None or stat.S_ISLNK(st.st_mode) \
                    or st.st_uid != 0 or os.path.realpath(os.path.dirname(p)) != os.path.dirname(p) \
                    or 'Linux mode' not in sh(SYNOACL, '-get', p, check=False, merge=True):
                skipped += 1
                continue
            if not plan:
                sh(SYNOACL, '-enforce-inherit', p)
            fixed += 1
        log('права: %s наследование ACL у %d объект(ов) root; не трогаю: %d' % (
            'вернул бы' if plan else 'вернул', fixed, skipped))

    def check_access(self, strict):
        """Всё в папках, куда run_as может писать, ему доступно; что можно, чиним.
        strict (первое переключение на run_as): иначе не переключаемся."""
        data = self.policy.data
        try:
            r = as_user(self.pw, scan_access, data)
            if r['bad'] and not self.dry:
                self.fix_access(r['bad'])
                r = as_user(self.pw, scan_access, data)
        except Failed as e:
            if strict:
                raise Stop(3, str(e), 'access')
            log('ВНИМАНИЕ: %s' % e)
            return
        log('доступ %s: пишет в %s; проверено %d, недоступно %d' % (
            self.run_as, ', '.join(r['tops']) or '-', r['total'], len(r['bad'])))
        for rel in r['bad'][:5]:
            log('  ✗ ' + rel, stamp=False)
        if r['bad'] and self.dry:
            self.fix_access(r['bad'], plan=True)
        elif r['bad'] and strict:
            raise Stop(3, 'у %s нет доступа к части файлов, на него не переключаю' % self.run_as)
        elif r['bad']:
            log('ВНИМАНИЕ: обновляю всё равно, проверка здоровья покажет')

    # --- между обновлениями: живы ли контейнеры, самолечение
    def watch(self, cur):
        """Контейнеры текущей версии не running/healthy — поднять заново тем же JSON;
        не поднялись — Stop(2, kind=health): письмо и отчёт один раз в 6 ч."""
        cs = project_containers(self.project)
        sick = sick_containers(cs)
        if cs and not sick:
            return
        names = ', '.join(c['Name'].lstrip('/') for c in sick) or 'контейнеров нет'
        log('контейнеры не в порядке: %s — поднимаю заново' % names)
        for c in sick:
            for line in sh(DOCKER, 'logs', '--tail', '15', c['Id'], check=False, merge=True).splitlines():
                log('    | ' + line, stamp=False)
        try:
            self.dc('up', '-d', '--remove-orphans')
        except Failed:
            self.dc('up', '-d', '--remove-orphans')
        if wait_healthy(self.project, self.verify_sec, 0, baseline=restart_counts(self.project)):
            self.outcome = {'event': 'heal', 'message': 'контейнеры упали (%s), перезапущены, все живы' % names,
                            'commit': cur['commit'], 'image': cur['image']}
            return
        raise Stop(2, 'контейнеры не поднимаются после перезапуска: %s' % names, 'health')

    # --- что сейчас работает
    def current(self):
        cur = read_json(self.current_file)
        deploy = read_json(self.deploy_file)
        if cur and deploy:
            cur['deploy'] = deploy
            return cur
        return None

    def remember(self, commit, image_id, deploy, key):
        write_json(self.deploy_file, deploy, private=True)
        write_json(self.current_file, {'commit': commit, 'image': image_id, 'key': key})
        sh(DOCKER, 'tag', image_id, self.deployed_tag)  # образ для отката не удалится

    def adopt(self):
        """Первый запуск: записей нет; считаем, что работает HEAD с локальным
        образом. Стек не трогаем."""
        head = self.git('rev-parse', 'HEAD').strip()
        image_id, _ = self.image_info(self.image)
        if not image_id:
            raise Stop(2, 'нет локального образа %s' % self.image, 'adopt')
        try:
            deploy = self.prepare(head)
        except Stop as e:
            raise Stop(2, 'текущий чекаут не проходит проверку: %s' % e, 'adopt')
        self.remember(head, image_id, deploy, self.version_key(head, image_id))
        log('первый запуск: считаю, что работает %s с образом %s' % (head[:12], image_id[7:19]))
        return self.current()

    # --- сценарий
    def run(self):
        self.check_user()
        new = self.fetch()
        if self.dry:
            log('в %s: %s' % (self.branch, self.describe(new)))
            self.prepare(new)
            self.check_access(strict=False)
            require_snapshot(self.policy.data, self.snapshot_max_age)
            log('dry-run: обновление было бы разрешено')
            return 0

        cur = self.current() or self.adopt()
        try:
            sh(DOCKER, 'pull', '-q', self.image)
        except Failed as e:
            raise Stop(3, 'docker pull не прошёл: %s' % e, 'network')
        new_id, rev = self.image_info(self.image)
        if not rev:
            raise Stop(3, 'у образа нет метки org.opencontainers.image.revision', 'image')
        if rev != new:
            log('образ для %s ещё не опубликован (в реестре %s), подожду' % (new[:12], rev[:12]))
            return 0
        self.key = self.version_key(new, new_id)
        if self.key == cur['key']:
            log('изменений нет')
            self.watch(cur)
            return 0
        if self.rejected():
            log('эту версию уже отклонил: %s. Жду новый коммит, образ или правку' % self.rejected())
            self.watch(cur)
            return 0

        log('новое: %s, образ %s' % (self.describe(new), new_id[7:19]))
        deploy = self.prepare(new)
        if (new, new_id, deploy) == (cur['commit'], cur['image'], cur['deploy']):
            self.remember(new, new_id, deploy, self.key)
            log('изменился только nas-update, стек тот же')
            return 0
        require_snapshot(self.policy.data, self.snapshot_max_age)
        self.check_access(strict=not run_as_services(cur['deploy'], self.user))

        log('обновляю')
        if self.apply(new, new_id, deploy):
            self.remember(new, new_id, deploy, self.key)
            remove_old_images([self.policy.image_repo], [new_id])
            log('готово')
            # для отчёта о версии на сайт (report.py)
            self.outcome = {'event': 'deploy', 'message': 'обновлено до %s' % new[:12], 'commit': new,
                            'prev_commit': cur['commit'], 'image': new_id}
            return 0
        log('новая версия не поднялась, откатываюсь на %s' % cur['commit'][:12])
        self.outcome = {'event': 'rollback', 'commit': cur['commit'], 'prev_commit': new, 'image': cur['image']}
        if self.apply(cur['commit'], cur['image'], cur['deploy']):
            raise Stop(4, 'новая версия упала, откат удался: работает прежняя')
        raise Stop(5, 'ОТКАТ ТОЖЕ НЕ ПОДНЯЛСЯ, нужен ручной разбор', 'broken')
