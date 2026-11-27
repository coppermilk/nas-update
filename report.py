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

  outbox_dir      = /volume1/@docker/volumes/minion2_aggregator-state/_data/outbox

outbox_dir (необязательно) — общий канал контейнер → сайт: каждый файл
<вид>.json в этой папке (вид — ^[a-z0-9-]{1,32}$, объект JSON с v: 1, до
OUTBOX_LIMIT знаков) уходит PUT на <url>/<вид> тем же ключом, когда меняется
его смысл (generated_at в отпечаток не входит). Какие виды принимать, решает
сайт (неизвестный — 422, строка в журнал, повтор через сутки или когда файл
изменится), поэтому новый вид сообщения — это PR в приложение и в сайт, без
правки conf и nas-update. Отпечатки — state/<имя>/outbox-sent.json.

События: deploy (обновилось), rollback (новая версия упала, откат удался),
heal (контейнеры упали, перезапущены и живы), error (заблокировано, нет
снапшота, сбой, откат или перезапуск не поднялся), heartbeat (раз в
heartbeat_hours: «жив, стоит такой-то коммит»). Сайт недоступен — отчёт ждёт в
state/<имя>/report-outbox.jsonl и досылается при следующем запуске. Отчёт
никогда не ломает обновление: любая ошибка — одна строка в журнал.

В heal и error о больных контейнерах в details уходит containers: [{name,
status, exit_code, restarts, log_tail}] — последние LOG_TAIL строк журнала,
похожее на токены и ключи заменено на *** (core.redact). Причина важнее
сводки каталога: не влезает в DETAILS_LIMIT — сначала уходит каталог, потом
старые строки журнала.

Совместимо с Python 3.8 (системный python3 в DSM 7).
"""
import hashlib
import json
import os
import re
import socket
import stat
import time
import urllib.error
import urllib.request

from core import log, read_json, write_json

EVENTS = ('deploy', 'rollback', 'heal', 'heartbeat', 'error')  # EVENTS сайта (workers/go/src/versions.mjs)
TIMEOUT = 20
DETAILS_LIMIT = 4000  # LIMITS.details сайта (workers/go/src/versions.mjs): длиннее — обрезанный и уже не JSON
SUMMARY_MAX_BYTES = 2 * 1024 * 1024
SUMMARY_SOURCES = ('set', 'channel', 'constants')
NAME_MAX = 64
OUTBOX_LIMIT = 16000  # BODY_LIMIT сайта (workers/go/src/inbox.mjs)
OUTBOX_FILES = 16  # файлов за запуск: папку пишет контейнер, root не должен по его воле слать без конца
OUTBOX_RETRY_SEC = 24 * 3600  # отвергнутое сайтом (4xx) не шлём снова, пока файл тот же, — раз в сутки
OUTBOX_NAME = re.compile(r'^([a-z0-9-]{1,32})\.json$')
OUTBOX_VERSION = 1


def utc_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def js_length(value):
    """Длина JSON.stringify(value) в JS (единицы UTF-16) — так сайт меряет details."""
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return len(text.encode('utf-16-le')) // 2


def read_untrusted_json(path, dir_fd=None):
    """JSON из файла, который пишет контейнер: не ссылка, не FIFO, не больше SUMMARY_MAX_BYTES.

    Бросает OSError или ValueError с причиной: root не должен ни зависнуть на FIFO,
    ни прочитать по ссылке чужой файл. dir_fd — открытая папка: path внутри неё.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
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


def meaning_digest(body):
    """Отпечаток смысла файла: generated_at — время пересборки, не новость (иначе каждый перезапуск — новая отправка)."""
    meaning = {k: v for k, v in body.items() if k != 'generated_at'}
    return hashlib.sha256(json.dumps(meaning, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def read_outbox(path, limit=OUTBOX_FILES):
    """[(вид, тело или исключение)] файлов <вид>.json папки path, по имени, не больше limit.

    Папка, файл-ссылка и не обычный файл не читаются: папку пишет контейнер. Файлы
    открываются относительно открытой папки, так что подмена папки ссылкой между
    списком и чтением ничего не даст. Бросает OSError, если папку не открыть.
    """
    dir_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        kinds = sorted(m.group(1) for m in map(OUTBOX_NAME.match, os.listdir(dir_fd)) if m)
        out = []
        for kind in kinds[:limit]:
            try:
                out.append((kind, read_untrusted_json(kind + '.json', dir_fd=dir_fd)))
            except (OSError, ValueError, RecursionError) as e:
                out.append((kind, e))
        if len(kinds) > limit:
            log('outbox: файлов %d, за запуск шлю %d по имени: %s' % (len(kinds), limit, ', '.join(kinds[limit:])))
        return out
    finally:
        os.close(dir_fd)


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
    """Уложить details в limit: сначала наборы каталога с конца — в счётчик sets_more («ещё N»).

    Причина падения важнее каталога: если с containers всё ещё длинно, каталог уходит
    целиком, потом — старые строки log_tail самого длинного хвоста.
    """
    if 'emoji_catalog' in details:
        fit_catalog(details, limit)
    if js_length(details) > limit and details.get('containers'):
        details.pop('emoji_catalog', None)
        tails = [c['log_tail'] for c in details['containers']]
        while js_length(details) > limit and any(tails):
            max(tails, key=len).pop(0)
    return details


def fit_catalog(details, limit):
    """Наборы details['emoji_catalog'] с конца — в sets_more, пока details длиннее limit."""
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
        self.outbox_dir = ''
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
        self.outbox_dir = section.get('outbox_dir', '').strip()
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
        """details отчёта: больные контейнеры (app.containers от watch) и сводка summary_file; нечего — None."""
        details = {}
        containers = getattr(self.app, 'containers', None)
        if containers:  # копия: fit_details укорачивает хвосты на месте
            details['containers'] = [dict(c, log_tail=list(c['log_tail'])) for c in containers]
        catalog = self.catalog()
        if catalog:
            details['emoji_catalog'] = catalog
        return fit_details(details) if details else None

    def catalog(self):
        """Сводка summary_file; без ключа или с битым файлом — None и строка в журнал."""
        if not self.summary_file:
            return None
        try:
            return catalog_summary(read_untrusted_json(self.summary_file))
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
            # без generated_at в отпечатке: иначе каждый перезапуск контейнера ложился бы новой строкой в историю сайта (50 строк)
            digest = meaning_digest(body)
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

    def outbox(self):
        """Файлы outbox_dir → PUT <url>/<вид>, когда смысл файла изменился. Сколько ушло; никогда не бросает."""
        if not (self.enabled and self.outbox_dir):
            return 0
        try:
            files = read_outbox(self.outbox_dir)
        except OSError as e:
            log('outbox: папка %s не открыта (%s) — не шлю' % (self.outbox_dir, e))
            return 0
        try:
            mark = self.app.path('outbox-sent.json')
            sent = read_json(mark, {})
            sent = sent if isinstance(sent, dict) else {}
            done = 0
            for kind, body in files:
                if self._send_one(kind, body, sent):
                    done += 1
            write_json(mark, sent)
            return done
        except Exception as e:  # ни при каких условиях не ломать обновление
            log('outbox: отправка не удалась: %r' % e)
            return 0

    def _send_one(self, kind, body, sent):
        """Один файл outbox; итог — в sent[kind] ({sha256, at, code}), когда сайт ответил окончательно."""
        problem = (str(body) if isinstance(body, Exception) else 'не объект JSON' if not isinstance(body, dict)
                   else 'нет v: %d' % OUTBOX_VERSION if body.get('v') != OUTBOX_VERSION
                   else 'длиннее %d знаков' % OUTBOX_LIMIT if js_length(body) > OUTBOX_LIMIT else '')
        if problem:
            log('outbox: %s.json не шлю (%s)' % (kind, problem))
            return False
        digest = meaning_digest(body)
        last = sent.get(kind) if isinstance(sent.get(kind), dict) else {}
        if last.get('sha256') == digest:
            accepted = 200 <= int(last.get('code') or 0) < 300
            if accepted or time.time() - float(last.get('at') or 0) < OUTBOX_RETRY_SEC:
                return False
        code = self.send({'host': self.host, 'sent_at': utc_now(), 'body': body}, '%s/%s' % (self.url, kind))
        reply = (' — ' + self.last_reply) if self.last_reply else ''
        if 200 <= code < 300:
            log('outbox: %s → сайт принял' % kind)
        elif code in (0, 401, 429) or code >= 500:  # сеть, ключ или сайт — не вина файла: повтор при следующем запуске
            log('outbox: %s — сайт %s (%d)%s, повторю при следующем запуске' % (kind, 'недоступен' if code == 0 else 'отверг', code, reply))
            return False
        else:  # 4xx про сам файл (422 — вид сайт не принимает): тот же файл — не раньше чем через OUTBOX_RETRY_SEC
            log('outbox: %s — сайт отверг (%d)%s; тот же файл повторю через сутки' % (kind, code, reply))
        sent[kind] = {'sha256': digest, 'at': time.time(), 'code': code}
        return 200 <= code < 300

    def after_run(self, code):
        """После app.run() без Stop: outbox, пересылка relay_file и отчёт об обновлении, иначе heartbeat."""
        self.outbox()
        self.relay()
        outcome = getattr(self.app, 'outcome', None)
        if outcome:
            o = dict(outcome)
            event = o.pop('event', 'deploy')
            self.post(event, event in ('deploy', 'heal'), o.pop('message', 'обновлено'), **o)
            return
        if code == 0:
            self.heartbeat()

    def stopped(self, e, rc):
        """После Stop: outbox и пересылка relay_file; отчёт — только о новых проблемах (rc != 0 — письмо тоже идёт один раз)."""
        self.outbox()
        self.relay()
        if rc == 0:
            return
        outcome = getattr(self.app, 'outcome', None) or {}
        o = {k: v for k, v in outcome.items() if k not in ('event', 'message')}
        event = 'rollback' if e.code == 4 else 'error'
        self.post(event, False, str(e), **o)
