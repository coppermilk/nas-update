"""Отчёт о версии на сайт: раздел «Версии» админки daetotyoma.com.

Облако не видит NAS, поэтому nas-update сам сообщает, что стоит и чем кончилось
обновление. Отчёт шлёт только этот код (из репозиториев приложений ничего не
исполняется), настраивается в apps/<имя>.conf:

  [report]
  url             = https://daetotyoma.com/admin/versions/minion2-nas
  key_file        = /volume1/docker/nas-update/secrets/versions.key   # или key = …
  heartbeat_hours = 24
  summary_file    = /volume1/@docker/volumes/minion2_aggregator-state/_data/emoji_catalog.json

summary_file (необязательно) — каталог премиум-эмодзи юзербота (emoji_catalog.json
из minion2): его краткая сводка уходит в details каждого отчёта, чтобы команда
видела наборы через MCP versions/version_history. Файл пишет контейнер, то есть
недоверенный код: читаем без перехода по ссылкам, только обычный файл до
SUMMARY_MAX_BYTES, в отчёт — только числа и короткие строки. Файла нет или он
битый — отчёт уходит без сводки, строка в журнал.

  relay_file      = /volume1/@docker/volumes/minion2_aggregator-state/_data/tgedit_report.json
  relay_url       = https://daetotyoma.com/admin/versions/minion2-tgedit

relay_file + relay_url (необязательно, вместе) — файл-итог, который пишет
контейнер (tgedit_report.json редактора канала minion2), уходит как есть в
details отчёта другого проекта сайта, когда его содержимое изменилось, — тем же
ключом. Так сайт узнаёт, какие правки канала выполнены, а ключ сайта остаётся
у root, а не в контейнере. Читается так же осторожно; не объект JSON или
длиннее DETAILS_LIMIT — не шлём, строка в журнал. Не ушло — уйдёт при
следующем запуске (отпечаток отправленного — state/<имя>/report-relay.json).

События: deploy (обновилось), rollback (новая версия упала, откат удался),
error (заблокировано, нет снапшота, сбой, откат не поднялся), heartbeat (раз в
heartbeat_hours: «жив, стоит такой-то коммит»). Сайт недоступен — отчёт ждёт в
state/<имя>/report-outbox.jsonl и досылается при следующем запуске. Отчёт
никогда не ломает обновление: любая ошибка — одна строка в журнал.

Совместимо с Python 3.8 (системный python3 в DSM 7).
"""
import hashlib
import json
import os
import socket
import stat
import time
import urllib.error
import urllib.request

from core import log, read_json, write_json

EVENTS = ('deploy', 'rollback', 'heartbeat', 'error')
TIMEOUT = 20
DETAILS_LIMIT = 4000  # LIMITS.details сайта (workers/go/src/versions.mjs): длиннее — обрезанный и уже не JSON
SUMMARY_MAX_BYTES = 2 * 1024 * 1024
SUMMARY_SOURCES = ('set', 'channel', 'constants')
NAME_MAX = 64


def utc_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def js_length(value):
    """Длина JSON.stringify(value) в JS (единицы UTF-16) — так сайт меряет details."""
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return len(text.encode('utf-16-le')) // 2


def read_untrusted_json(path):
    """JSON из файла, который пишет контейнер: не ссылка, не FIFO, не больше SUMMARY_MAX_BYTES.

    Бросает OSError или ValueError с причиной: root не должен ни зависнуть на FIFO,
    ни прочитать по ссылке чужой файл.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError('не обычный файл')
        if st.st_size > SUMMARY_MAX_BYTES:
            raise ValueError('больше %d байт' % SUMMARY_MAX_BYTES)
        data = os.read(fd, SUMMARY_MAX_BYTES + 1)
    finally:
        os.close(fd)
    return json.loads(data.decode('utf-8'))


def _name(value):
    return str(value or '')[:NAME_MAX]


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def catalog_summary(catalog):
    """Сводка emoji_catalog.json: наборы и число эмодзи по источникам, без самих эмодзи."""
    if not isinstance(catalog, dict):
        raise ValueError('каталог — не объект JSON')
    sets = catalog.get('sets') if isinstance(catalog.get('sets'), list) else []
    emoji = catalog.get('emoji') if isinstance(catalog.get('emoji'), list) else []
    by_source = dict.fromkeys(SUMMARY_SOURCES, 0)
    for row in emoji:
        sources = row.get('source') if isinstance(row, dict) else None
        for src in sources if isinstance(sources, list) else [sources]:
            if src in by_source:
                by_source[src] += 1
    return {
        'generated_at': _name(catalog.get('generated_at')),
        'sets_read': catalog.get('sets_read') is True,
        'sets_count': len(sets),
        'emoji_count': len(emoji),
        'emoji_by_source': by_source,
        'sets': [{'short_name': _name(one.get('short_name')), 'title': _name(one.get('title')),
                  'count': _count(one.get('count'))} for one in sets if isinstance(one, dict)],
    }


def fit_details(details, limit=DETAILS_LIMIT):
    """Уложить {'emoji_catalog': сводка} в limit: лишние наборы с конца — в счётчик sets_more («ещё N»)."""
    summary = details['emoji_catalog']
    total = len(summary['sets'])
    # пустой набор в JSON — {"short_name":"","title":"","count":0}, 38 знаков: больше limit // 38 не влезет
    # никогда, а срез сразу держит цикл ниже коротким и при тысячах наборов
    shown = summary['sets'][:limit // 38]
    while True:
        summary['sets'] = shown
        if len(shown) < total:
            summary['sets_more'] = total - len(shown)
        if js_length(details) <= limit or not shown:
            return details
        shown = shown[:-1]


class Reporter:
    """Один отчётчик на приложение; без [report] в конфиге молчит."""

    def __init__(self, app, section, dry=False, send=None):
        self.app = app
        self.dry = dry
        self.send = send or self._send
        self.url = self.key = ''
        self.last_reply = ''  # первая строка ответа сайта при отказе (4xx/5xx) — в лог, чтобы причина была видна
        self.heartbeat_hours = 24
        self.summary_file = ''
        self.relay_file = self.relay_url = ''
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
        self.summary_file = section.get('summary_file', '').strip()
        self.relay_file = section.get('relay_file', '').strip()
        self.relay_url = section.get('relay_url', '').strip().rstrip('/')
        if bool(self.relay_file) != bool(self.relay_url):
            log('отчёт о версии: relay_file и relay_url нужны вместе — пересылку не делаю')
            self.relay_file = self.relay_url = ''
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
        if 'details' not in body:
            details = self.details()
            if details:
                body['details'] = details
        return body

    def details(self):
        """Сводка summary_file для details; без ключа или с битым файлом — None и строка в журнал."""
        if not self.summary_file:
            return None
        try:
            return fit_details({'emoji_catalog': catalog_summary(read_untrusted_json(self.summary_file))})
        except (OSError, ValueError, RecursionError) as e:  # RecursionError — JSON глубокой вложенности
            log('отчёт о версии: summary_file %s не прочитан (%s) — отчёт без сводки' % (self.summary_file, e))
            return None

    # --- отправка
    def _send(self, body, url=None):
        """PUT на сайт (url — адрес отчёта, по умолчанию url из [report]); код ответа, 0 — сети нет."""
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(url or self.url, data=data, method='PUT', headers={
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

    def relay(self):
        """relay_file → details отчёта на relay_url, когда файл изменился. Никогда не бросает."""
        if not (self.enabled and self.relay_file):
            return False
        try:
            body = read_untrusted_json(self.relay_file)
            if not isinstance(body, dict):
                raise ValueError('не объект JSON')
            if js_length(body) > DETAILS_LIMIT:
                raise ValueError('длиннее %d знаков' % DETAILS_LIMIT)
        except (OSError, ValueError, RecursionError) as e:
            log('пересылка: relay_file %s не прочитан (%s) — не шлю' % (self.relay_file, e))
            return False
        try:
            mark = self.app.path('report-relay.json')
            # generated_at — время пересборки файла, не его смысл: движок пишет новое при каждом старте, и без этого
            # каждый перезапуск контейнера ложился бы новой строкой в историю отчётов сайта (она держит 50 строк)
            meaning = {k: v for k, v in body.items() if k != 'generated_at'}
            digest = hashlib.sha256(json.dumps(meaning, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
            if read_json(mark, {}).get('sha256') == digest:
                return False
            report = {'event': 'heartbeat', 'ok': True, 'host': self.host, 'finished_at': utc_now(),
                      'message': 'итог из %s' % os.path.basename(self.relay_file), 'details': body}
            report.update({k: v for k, v in self.info().items() if v})
            code = self.send(report, self.relay_url)
            if 200 <= code < 300:
                write_json(mark, {'sha256': digest, 'at': time.time()})
                log('пересылка: %s → сайт принял' % os.path.basename(self.relay_file))
                return True
            log('пересылка: сайт %s (%d)%s — повторю при следующем запуске' % (
                'недоступен' if code == 0 else 'отверг', code, (' — ' + self.last_reply) if self.last_reply else ''))
            return False
        except Exception as e:  # ни при каких условиях не ломать обновление
            log('пересылка не удалась: %r' % e)
            return False

    def after_run(self, code):
        """После app.run() без Stop: пересылка relay_file и отчёт об обновлении, иначе heartbeat."""
        self.relay()
        outcome = getattr(self.app, 'outcome', None)
        if outcome:
            o = dict(outcome)
            event = o.pop('event', 'deploy')
            self.post(event, event == 'deploy', o.pop('message', 'обновлено'), **o)
            return
        if code == 0:
            self.heartbeat()

    def stopped(self, e, rc):
        """После Stop: пересылка relay_file; отчёт — только о новых проблемах (rc != 0 — письмо тоже идёт один раз)."""
        self.relay()
        if rc == 0:
            return
        outcome = getattr(self.app, 'outcome', None) or {}
        o = {k: v for k, v in outcome.items() if k not in ('event', 'message')}
        event = 'rollback' if e.code == 4 else 'error'
        self.post(event, False, str(e), **o)
