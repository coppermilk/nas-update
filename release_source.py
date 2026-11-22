"""source = releases: стороннее приложение, которое выпускает релизы на GitHub
(например Immich). Издатель доверенный, но релиз может что-то сломать.

Запуск в окне расписания, раз в день:
  1. Какая версия работает (переменная окружения в образе) и какой major
     отслеживается (version_var=vN в .env приложения).
  2. Самый новый стабильный релиз этого major, которому уже cooldown_days
     (свежий x.y.0 часто чинят на следующий день).
  3. Стоп и письмо, если в заметках к релизу есть «breaking» или в
     эталонном compose из релиза изменился образ из stop_if_image.
  4. Нужен свежий снапшот шары, где лежит приложение.
  5. pull и up -d ровно этой версии (version_var подменяется только для
     команды, .env не меняется). Ждём, пока все контейнеры станут healthy
     и, если задан version_url, сервер ответит новой версией.
  6. Не поднялся: возвращаем прежнюю версию из локальной копии образа.
     Поднялся: удаляем старые образы.
Вышел новый major: одно письмо; переход — version_var=vN в .env.
"""
import datetime
import json
import os
import re

from core import (DOCKER, App, Failed, Stop, compose, http_get, log,
                  project_containers, read_json, remove_old_images, require_snapshot,
                  restart_counts, sh, wait_healthy, write_json)


def version(tag):
    """'v3.4.2' -> (3, 4, 2); не стабильный релиз -> None."""
    m = re.match(r'^v(\d+)\.(\d+)\.(\d+)$', tag or '')
    return tuple(int(x) for x in m.groups()) if m else None


def vtag(v):
    return 'v%d.%d.%d' % v


def image_of(compose_text, word):
    """Строка image: того сервиса, в образе которого есть word."""
    for line in compose_text.splitlines():
        m = re.match(r'\s*image:\s*(\S+)', line)
        if m and word in m.group(1):
            return m.group(1)
    return None


class Release:
    def __init__(self, r):
        self.tag = r['tag_name']
        self.v = version(self.tag)
        self.stable = bool(self.v) and not r.get('prerelease') and not r.get('draft')
        self.published = datetime.datetime.strptime(
            r['published_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
        self.notes = r.get('body') or ''

    def age_days(self):
        return (datetime.datetime.now(datetime.timezone.utc) - self.published).total_seconds() / 86400


class ReleaseApp(App):
    def __init__(self, name, c, dry):
        super().__init__(name, c, dry)
        try:
            self.dir = c['dir'].rstrip('/')
            self.project = c['project']
            self.files = [os.path.join(self.dir, f) for f in c['files'].split()]
            self.github = c['github']
            self.var = c['version_var']
            self.images = c['images'].split()       # первый — образ сервера
            self.server = c['server_container']
            self.version_env = c.get('version_env', '')
            self.version_url = c.get('version_url', '')
            self.reference = c.get('reference_compose', 'docker-compose.yml')
            self.stop_if_image = c.get('stop_if_image', '').split()
            self.cooldown = c.getfloat('cooldown_days', 3)
            self.settle = c.getint('settle_sec', 10)
            self.health_timeout = c.getint('health_timeout', 900)
        except (KeyError, ValueError) as e:
            raise Stop(2, '%s.conf: %s' % (name, e), 'config')

    def guarded_paths(self):
        return [self.dir]

    # --- что есть сейчас
    def tracked_major(self):
        env = {}
        with open(os.path.join(self.dir, '.env'), encoding='utf-8', errors='replace') as f:
            for line in f:
                k, _, v = line.strip().partition('=')
                if k and not k.startswith('#'):
                    env[k.strip()] = v.strip().strip('"\'')
        m = re.match(r'^v(\d+)(\.|$)', env.get(self.var, ''))
        if not m:
            raise Stop(2, 'в %s/.env нужен %s=vN (например v3), сейчас %r'
                       % (self.dir, self.var, env.get(self.var)), 'config')
        return int(m.group(1))

    def running(self):
        """Работающая версия и образы контейнеров приложения {контейнер: (repo, id)}."""
        cs = project_containers(self.project)
        server = [c for c in cs if c['Name'].lstrip('/') == self.server]
        if not server:
            raise Stop(2, 'нет контейнера %s' % self.server, 'config')
        env = dict(e.split('=', 1) for e in server[0]['Config'].get('Env') or [] if '=' in e)
        image = server[0]['Config'].get('Image', '')
        v = version(env.get(self.version_env)) if self.version_env else version(image.rsplit(':', 1)[-1])
        if not v:
            raise Stop(2, 'не понял версию работающего %s' % self.server, 'config')
        ours = {}
        for c in cs:
            repo = c['Config'].get('Image', '').rsplit(':', 1)[0]
            if repo in self.images:
                ours[c['Name'].lstrip('/')] = (repo, c['Image'])
        return v, ours

    def releases(self):
        try:
            data = json.loads(http_get('https://api.github.com/repos/%s/releases?per_page=30' % self.github))
        except (Failed, ValueError) as e:
            raise Stop(2, 'не получил список релизов с GitHub: %s' % e, 'network')
        stable = [r for r in map(Release, data) if r.stable]
        if not stable:
            raise Stop(2, 'на GitHub не нашёл ни одного стабильного релиза', 'network')
        return stable

    def server_version(self):
        """Версия, которой отвечает сам сервер (version_url), или None."""
        try:
            got = json.loads(http_get(self.version_url))
            if 'version' in got:
                return version('v' + str(got['version']).lstrip('v'))
            return got['major'], got['minor'], got['patch']
        except (Failed, ValueError, KeyError, TypeError, AttributeError):
            return None

    def dc(self, v, *args):
        return compose(self.project, self.files, *args, project_dir=self.dir,
                       env={self.var: vtag(v)})

    def switch(self, v):
        """Поднять версию v и дождаться здоровья."""
        before = restart_counts(self.project)
        try:
            self.dc(v, 'up', '-d')
        except Failed as e:
            log(str(e))
            return False
        check = (lambda: self.server_version() == v) if self.version_url else None
        return wait_healthy(self.project, self.settle, self.health_timeout, check, before)

    # --- что мешает поставить релиз автоматически
    def reasons_to_wait(self, r):
        why = []
        if re.search(r'breaking', r.notes, re.I):
            why.append('в заметках к релизу есть «breaking»')
        if self.stop_if_image:
            try:
                ref = http_get('https://github.com/%s/releases/download/%s/%s'
                               % (self.github, r.tag, self.reference))
            except Failed as e:
                raise Stop(2, 'не скачал эталонный %s: %s' % (self.reference, e), 'network')
            with open(self.files[0], encoding='utf-8', errors='replace') as f:
                mine = f.read()
            for word in self.stop_if_image:
                if image_of(ref, word) != image_of(mine, word):
                    why.append('в эталонном %s другой образ %s: %s' % (self.reference, word, image_of(ref, word)))
        return why

    def last_backup(self):
        """Свежий дамп базы, если приложение делает его само (только для журнала)."""
        d = os.path.join(self.dir, 'library', 'backups')
        try:
            files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith('.sql.gz')]
        except OSError:
            return ''
        if not files:
            return ''
        f = max(files, key=os.path.getmtime)
        return '; последний дамп базы: %s (%.0f ч назад)' % (
            os.path.basename(f), (datetime.datetime.now().timestamp() - os.path.getmtime(f)) / 3600)

    # --- сценарий
    def run(self):
        major = self.tracked_major()
        current, ours = self.running()
        stable = self.releases()

        newest = max(stable, key=lambda r: r.v)
        ripe = [r for r in stable if r.v[0] == major and r.age_days() >= self.cooldown]
        young = sorted(r.tag for r in stable if r.v[0] == major and r.v > current and r.age_days() < self.cooldown)
        target = max(ripe, key=lambda r: r.v) if ripe else None
        answer = self.server_version() if self.version_url else None
        log('работает %s%s, отслеживаю v%d%s' % (
            vtag(current), ' (сервер отвечает %s)' % (vtag(answer) if answer else 'ничем') if self.version_url else '',
            major, self.last_backup()))
        if young:
            log('свежие релизы ждут %g дн.: %s' % (self.cooldown, ' '.join(young)))

        code = self.notify_major(newest, major)
        if not target or target.v <= current:
            log('обновлять нечего')
            return code
        self.key = '%s %s' % (target.tag, self.salt())
        if self.rejected():
            log('%s уже отложена: %s' % (target.tag, self.rejected()))
            return code

        log('цель: %s (вышла %s, %.0f дн. назад)' % (target.tag, target.published.date(), target.age_days()))
        why = self.reasons_to_wait(target)
        if why:
            raise Stop(3, 'не ставлю %s сам: %s. Обнови вручную, когда разберёшься' % (target.tag, '; '.join(why)))
        require_snapshot(self.dir, self.snapshot_max_age)
        if self.dry:
            log('dry-run: обновил бы %s -> %s' % (vtag(current), target.tag))
            return code

        # Работающие образы — под точными тегами: откат без скачивания.
        for repo, image_id in ours.values():
            sh(DOCKER, 'tag', image_id, '%s:%s' % (repo, vtag(current)))
        log('скачиваю %s' % target.tag)
        try:
            self.dc(target.v, 'pull', '--quiet')
        except Failed as e:
            raise Stop(3, 'pull не прошёл: %s' % e, 'network')
        log('обновляю %s -> %s' % (vtag(current), target.tag))
        if self.switch(target.v):
            remove_old_images(self.images, [i for _, i in self.running()[1].values()])
            log('готово: %s' % target.tag)
            return code
        log('новая версия не поднялась, возвращаю %s' % vtag(current))
        if self.switch(current):
            raise Stop(4, '%s не поднялся, вернул %s' % (target.tag, vtag(current)))
        raise Stop(5, 'не поднялся ни %s, ни %s. Восстанови из снапшота шары с %s или из дампа базы'
                   % (target.tag, vtag(current), self.dir), 'broken')

    def notify_major(self, newest, major):
        """Вышел новый major: одно письмо (код 3), дальше только строка в журнале."""
        if newest.v[0] <= major:
            return 0
        log('вышла %s. Сам на новый major не перехожу: прочитай заметки к релизу, '
            'потом поставь %s=v%d в %s/.env' % (newest.tag, self.var, newest.v[0], self.dir))
        if self.dry or read_json(self.path('major.json')) == newest.v[0]:
            return 0
        write_json(self.path('major.json'), newest.v[0])
        return 3
