"""Отчёт о версии на сайт: раздел «Версии» админки daetotyoma.com.

Облако не видит NAS, поэтому nas-update сам сообщает, что стоит и чем кончилось
обновление. Отчёт шлёт только этот код (из репозиториев приложений ничего не
исполняется), настраивается в apps/<имя>.conf:

  [report]
  url             = https://daetotyoma.com/admin/versions/minion2-nas
  key_file        = /volume1/docker/nas-update/secrets/versions.key   # или key = …
  heartbeat_hours = 24

События: deploy (обновилось), rollback (новая версия упала, откат удался),
error (заблокировано, нет снапшота, сбой, откат не поднялся), heartbeat (раз в
heartbeat_hours: «жив, стоит такой-то коммит»). Сайт недоступен — отчёт ждёт в
state/<имя>/report-outbox.jsonl и досылается при следующем запуске. Отчёт
никогда не ломает обновление: любая ошибка — одна строка в журнал.

Совместимо с Python 3.8 (системный python3 в DSM 7).
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

from core import log, read_json, write_json

EVENTS = ('deploy', 'rollback', 'heartbeat', 'error')
TIMEOUT = 20


def utc_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class Reporter:
    """Один отчётчик на приложение; без [report] в конфиге молчит."""

    def __init__(self, app, section, dry=False, send=None):
        self.app = app
        self.dry = dry
        self.send = send or self._send
        self.url = self.key = ''
        self.last_reply = ''  # первая строка ответа сайта при отказе (4xx/5xx) — в лог, чтобы причина была видна
        self.heartbeat_hours = 24
        self.host = 'nas-' + (socket.gethostname() or 'nas')
        if section is None:
            return
        self.url = section.get('url', '').strip().rstrip('/')
        key = section.get('key', '').strip()
        key_file = section.get('key_file', '').strip()
        if not key and key_file:
            try:
                with open(key_file, encoding='utf-8') as f:
                    key = f.read().strip()
            except OSError as e:
                log('отчёт о версии: key_file не прочитан (%s)' % e)
        self.key = key
        self.host = section.get('host', self.host).strip() or self.host
        try:
            self.heartbeat_hours = float(section.get('heartbeat_hours', '24'))
        except ValueError:
            self.heartbeat_hours = 24
        if self.url and not self.key:
            log('отчёт о версии: в [report] нет key или key_file — отчёты не шлю')

    @property
    def enabled(self):
        return bool(self.url and self.key and not self.dry)

    # --- что сейчас стоит (подклассы App отдают version_info())
    def info(self):
        fn = getattr(self.app, 'version_info', None)
        try:
            return dict(fn() or {}) if fn else {}
        except Exception as e:  # отчёт не должен падать из-за состояния приложения
            log('отчёт о версии: состояние не прочитано (%r)' % e)
            return {}

    def payload(self, event, ok, message, **extra):
        body = {'event': event, 'ok': bool(ok), 'host': self.host, 'message': str(message or '')[:500],
                'finished_at': utc_now()}
        body.update({k: v for k, v in self.info().items() if v})
        body.update({k: v for k, v in extra.items() if v is not None})
        return body

    # --- отправка
    def _send(self, body):
        """PUT на сайт; код ответа, 0 — сети нет."""
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(self.url, data=data, method='PUT', headers={
            'Content-Type': 'application/json; charset=utf-8', 'X-Board-Key': self.key,
            'User-Agent': 'nas-update'})
        self.last_reply = ''
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status
        except urllib.error.HTTPError as e:
            try:
                self.last_reply = e.read(400).decode('utf-8', 'replace').strip().splitlines()[0][:200]
            except Exception:
                self.last_reply = ''
            return e.code
        except (urllib.error.URLError, OSError, socket.timeout):
            return 0

    def outbox_path(self):
        return self.app.path('report-outbox.jsonl')

    def flush(self):
        """Дослать отложенное; что снова не ушло — оставить."""
        path = self.outbox_path()
        if not os.path.exists(path):
            return
        with open(path, encoding='utf-8') as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        rest = []
        for line in lines:
            try:
                body = json.loads(line)
            except ValueError:
                continue
            if self.send(body) == 0:
                rest.append(line)
        if rest:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(rest) + '\n')
        else:
            os.remove(path)

    def post(self, event, ok, message, **extra):
        """Отправить отчёт; без сети — отложить. Никогда не бросает."""
        if event not in EVENTS or not self.enabled:
            return False
        try:
            self.flush()
            body = self.payload(event, ok, message, **extra)
            code = self.send(body)
            if 200 <= code < 300:
                log('отчёт о версии: %s → сайт принял' % event)
                return True
            if code == 0:
                with open(self.outbox_path(), 'a', encoding='utf-8') as f:
                    f.write(json.dumps(body, ensure_ascii=False) + '\n')
                log('отчёт о версии: сайт недоступен, %s отложен' % event)
                return False
            log('отчёт о версии: сайт отверг %s (%d)%s' % (event, code, (' — ' + self.last_reply) if self.last_reply else ''))
            if code == 401:
                log('отчёт о версии: ключ из key_file не совпал с секретом VERSIONS_KEY сайта или секрет на сайте не задан — '
                    'сверь значение (без пробелов и переносов) и Cloudflare → Pages → Variables and Secrets → Production')
            elif code == 404:
                log('отчёт о версии: проверь url в [report] — сайт не знает такой адрес')
            return False
        except Exception as e:  # ни при каких условиях не ломать обновление
            log('отчёт о версии не отправлен: %r' % e)
            return False

    def heartbeat(self):
        """Раз в heartbeat_hours: «жив, стоит такой-то коммит»."""
        if not self.enabled:
            return False
        last = read_json(self.app.path('report-heartbeat.json'), {}).get('at', 0)
        if time.time() - float(last or 0) < self.heartbeat_hours * 3600:
            return False
        if self.post('heartbeat', True, 'жив, изменений нет'):
            write_json(self.app.path('report-heartbeat.json'), {'at': time.time()})
            return True
        return False

    def after_run(self, code):
        """После app.run() без Stop: отчёт об обновлении, иначе heartbeat."""
        outcome = getattr(self.app, 'outcome', None)
        if outcome:
            o = dict(outcome)
            event = o.pop('event', 'deploy')
            self.post(event, event == 'deploy', o.pop('message', 'обновлено'), **o)
            return
        if code == 0:
            self.heartbeat()

    def stopped(self, e, rc):
        """После Stop: только о новых проблемах (rc != 0 — письмо тоже идёт один раз)."""
        if rc == 0:
            return
        outcome = getattr(self.app, 'outcome', None) or {}
        o = {k: v for k, v in outcome.items() if k not in ('event', 'message')}
        event = 'rollback' if e.code == 4 else 'error'
        self.post(event, False, str(e), **o)
