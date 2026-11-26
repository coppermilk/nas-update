"""Отчёт о версии на сайт (report.py): без root и сети — отправку подменяет заглушка.

    python3 tests/test_report.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import core  # noqa: E402
from core import Stop  # noqa: E402
from report import DETAILS_LIMIT, OUTBOX_FILES, OUTBOX_LIMIT, OUTBOX_RETRY_SEC, Reporter, fit_details, catalog_summary, js_length  # noqa: E402

CATALOG = os.path.join(HERE, 'fixtures', 'emoji_catalog.json')

TMP = tempfile.mkdtemp(prefix='nas-update-report-')
core.STATE = TMP


class FakeApp:
    def __init__(self, name='minion2'):
        self.state = os.path.join(TMP, name)
        os.makedirs(self.state, exist_ok=True)
        self.outcome = None

    def path(self, name):
        return os.path.join(self.state, name)

    def version_info(self):
        return {'commit': 'abc1234567890', 'image': 'sha256:deadbeef', 'branch': 'main'}


class Section(dict):
    pass


sent = []
codes = []


relayed = []


def fake_send(body, url=None):
    (relayed if url else sent).append((url, body) if url else body)
    return codes.pop(0) if codes else 201


def reporter(app, **over):
    sec = Section(url='https://daetotyoma.com/admin/versions/minion2-nas', key='k', heartbeat_hours='24')
    sec.update(over)
    return Reporter(app, sec, send=fake_send)


def test_disabled_without_section():
    app = FakeApp('a')
    r = Reporter(app, None, send=fake_send)
    n = len(sent)
    assert not r.enabled and r.post('deploy', True, 'x') is False and len(sent) == n


def test_payload_and_deploy():
    app = FakeApp('b')
    app.outcome = {'event': 'deploy', 'message': 'обновлено до abc1234', 'commit': 'abc1234567890',
                   'prev_commit': 'old', 'image': 'sha256:deadbeef'}
    r = reporter(app)
    r.after_run(0)
    assert sent[-1]['event'] == 'deploy' and sent[-1]['ok'] is True
    assert sent[-1]['commit'] == 'abc1234567890' and sent[-1]['prev_commit'] == 'old'
    assert sent[-1]['host'].startswith('nas-') and sent[-1]['finished_at'].endswith('Z')


def test_rollback_and_error_only_new():
    app = FakeApp('c')
    app.outcome = {'event': 'rollback', 'commit': 'old', 'prev_commit': 'new'}
    r = reporter(app)
    r.stopped(Stop(4, 'новая версия упала, откат удался'), 4)
    assert sent[-1]['event'] == 'rollback' and sent[-1]['ok'] is False and sent[-1]['commit'] == 'old'
    n = len(sent)
    r.stopped(Stop(3, 'заблокировано'), 0)  # повтор, письмо не шлётся — и отчёт тоже
    assert len(sent) == n
    r.stopped(Stop(3, 'заблокировано: политика'), 3)
    assert sent[-1]['event'] == 'error' and 'политика' in sent[-1]['message']


def test_heartbeat_once_per_period():
    app = FakeApp('d')
    r = reporter(app, heartbeat_hours='1')
    assert r.heartbeat() is True and sent[-1]['event'] == 'heartbeat'
    assert r.heartbeat() is False, 'второй раз в тот же час — молчим'
    core.write_json(app.path('report-heartbeat.json'), {'at': time.time() - 2 * 3600})
    assert r.heartbeat() is True


def test_outbox_when_offline():
    app = FakeApp('e')
    r = reporter(app)
    codes.extend([0])  # сети нет
    assert r.post('deploy', True, 'x') is False
    assert os.path.exists(r.outbox_path())
    codes.extend([201, 201])  # сеть вернулась: сначала отложенное, потом новое
    assert r.post('heartbeat', True, 'y') is True
    assert not os.path.exists(r.outbox_path())
    assert [b['event'] for b in sent[-2:]] == ['deploy', 'heartbeat']


def test_rejected_not_queued(capsys=None):
    app = FakeApp('f')
    r = reporter(app)
    codes.extend([401])
    assert r.post('deploy', True, 'x') is False and not os.path.exists(r.outbox_path())


def test_rejected_logs_site_reply():
    # сайт ответил 401 с текстом — текст и подсказка про VERSIONS_KEY попадают в лог, а не глотаются
    import io
    import contextlib
    app = FakeApp('g')
    r = reporter(app)

    def send_401(body):
        r.last_reply = 'На сайте не задан секрет VERSIONS_KEY'
        return 401
    r.send = send_401
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert r.post('deploy', True, 'x') is False
    text = out.getvalue()
    assert 'отверг deploy (401) — На сайте не задан секрет VERSIONS_KEY' in text and 'VERSIONS_KEY' in text


def test_dry_run_silent():
    app = FakeApp('g')
    sec = Section(url='https://x', key='k')
    r = Reporter(app, sec, dry=True, send=fake_send)
    n = len(sent)
    r.after_run(0)
    assert len(sent) == n


def test_summary_from_catalog_fixture():
    app = FakeApp('s1')
    r = reporter(app, summary_file=CATALOG)
    r.after_run(0)
    d = sent[-1]['details']['emoji_catalog']
    assert d['sets_read'] is True and d['generated_at'] == '2026-11-26T13:00:00Z'
    assert d['sets_count'] == 2 and d['emoji_count'] == 5
    assert d['emoji_by_source'] == {'set': 3, 'channel': 2, 'constants': 1}, 'эмодзи в двух источниках считается в обоих'
    assert d['sets'] == [{'short_name': 'TyomaPack', 'title': 'Tyoma', 'count': 2},
                         {'short_name': 'CatsVienna', 'title': 'Котики Вены', 'count': 1}]
    assert 'sets_more' not in d
    assert all('id' not in one for one in d['sets']), 'в отчёт идут наборы, а не сами эмодзи'


def test_summary_on_deploy_too():
    app = FakeApp('s2')
    app.outcome = {'event': 'deploy', 'message': 'обновлено', 'commit': 'abc'}
    reporter(app, summary_file=CATALOG).after_run(0)
    assert sent[-1]['event'] == 'deploy' and sent[-1]['details']['emoji_catalog']['sets_count'] == 2


def test_no_summary_key_no_details():
    app = FakeApp('s3')
    reporter(app).after_run(0)
    assert 'details' not in sent[-1]


def _report_with_file(name, content):
    import io
    import contextlib
    app = FakeApp(name)
    path = app.path('catalog.json')
    if content is not None:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        reporter(app, summary_file=path).after_run(0)
    return sent[-1], out.getvalue()


def test_missing_file_report_goes_without_summary():
    body, logged = _report_with_file('s4', None)
    assert body['event'] == 'heartbeat' and 'details' not in body
    assert 'summary_file' in logged and 'без сводки' in logged


def test_broken_json_report_goes_without_summary():
    body, logged = _report_with_file('s5', '{"sets": [')
    assert body['event'] == 'heartbeat' and 'details' not in body and 'без сводки' in logged


def test_not_an_object_report_goes_without_summary():
    body, logged = _report_with_file('s6', '[1, 2]')
    assert 'details' not in body and 'не объект' in logged


def test_deep_nesting_report_goes_without_summary():
    body, logged = _report_with_file('s7', '[' * 100000)
    assert 'details' not in body and 'без сводки' in logged


def test_symlink_not_followed():
    # файл пишет контейнер: ссылка на чужой файл хоста не читается
    app = FakeApp('s8')
    link = app.path('link.json')
    os.symlink(CATALOG, link)
    reporter(app, summary_file=link).after_run(0)
    assert 'details' not in sent[-1]


def test_fifo_does_not_hang():
    app = FakeApp('s9')
    fifo = app.path('fifo.json')
    os.mkfifo(fifo)
    reporter(app, summary_file=fifo).after_run(0)
    assert 'details' not in sent[-1]


def _many_sets(n):
    return {'generated_at': 'x', 'sets_read': True, 'emoji': [],
            'sets': [{'short_name': 'Set%03d' % i, 'title': 'Котики и эмодзи 🐰 %03d' % i, 'count': i} for i in range(n)]}


def test_fit_trims_sets_to_limit():
    details = fit_details({'emoji_catalog': catalog_summary(_many_sets(200))})
    d = details['emoji_catalog']
    assert js_length(details) <= DETAILS_LIMIT
    assert d['sets_count'] == 200 and d['sets_more'] == 200 - len(d['sets']) > 0
    assert d['sets'][0]['short_name'] == 'Set000', 'обрезается хвост, начало списка на месте'
    with_one_more = {'emoji_catalog': dict(d, sets=d['sets'] + [{'short_name': 'x', 'title': 'x', 'count': 1}])}
    assert js_length(with_one_more) > DETAILS_LIMIT - 30, 'обрезано не больше нужного'


def test_fit_exact_limit_untouched():
    details = {'emoji_catalog': catalog_summary(_many_sets(3))}
    size = js_length(details)
    assert 'sets_more' not in fit_details(details, limit=size)['emoji_catalog']
    assert fit_details(details, limit=size - 1)['emoji_catalog']['sets_more'] == 1


def test_fit_thousands_of_sets_fast():
    started = time.time()
    details = fit_details({'emoji_catalog': catalog_summary(_many_sets(20000))})
    assert js_length(details) <= DETAILS_LIMIT and details['emoji_catalog']['sets_more'] > 19000
    assert time.time() - started < 2


def test_js_length_counts_utf16():
    assert js_length('🐰') == 4 and js_length('ж') == 3  # кавычки + 2 единицы UTF-16 у 🐰, 1 у ж


def test_long_names_capped():
    cat = {'sets': [{'short_name': 'a' * 500, 'title': 'б' * 500, 'count': -5}], 'emoji': [{'source': 'set'}]}
    d = catalog_summary(cat)
    assert len(d['sets'][0]['short_name']) == 64 and len(d['sets'][0]['title']) == 64 and d['sets'][0]['count'] == 0
    assert d['emoji_by_source']['set'] == 1


def test_sick_containers():
    from core import sick_containers
    run = {'Id': '1', 'Name': '/a', 'State': {'Status': 'running'}, 'RestartCount': 0}
    unhealthy = {'Id': '2', 'Name': '/b', 'State': {'Status': 'running', 'Health': {'Status': 'unhealthy'}}, 'RestartCount': 0}
    exited = {'Id': '3', 'Name': '/c', 'State': {'Status': 'exited'}, 'RestartCount': 0}
    restarted = {'Id': '4', 'Name': '/d', 'State': {'Status': 'running'}, 'RestartCount': 2}
    assert sick_containers([run]) == []
    assert [c['Id'] for c in sick_containers([run, unhealthy, exited, restarted])] == ['2', '3', '4']
    assert sick_containers([restarted], baseline={'4': 2}) == []


TGEDIT = os.path.join(HERE, 'fixtures', 'tgedit_report.json')
RELAY_URL = 'https://daetotyoma.com/admin/versions/minion2-tgedit'


def _relayer(name, path=TGEDIT):
    app = FakeApp(name)
    return app, reporter(app, relay_file=path, relay_url=RELAY_URL)


def test_relay_sends_file_as_details_to_its_project():
    _, r = _relayer('r1')
    n = len(sent)
    r.after_run(0)
    url, body = relayed[-1]
    assert url == RELAY_URL and body['event'] == 'heartbeat' and body['ok'] is True
    assert [p['id'] for p in body['details']['packages']] == ['576-viewer-pin', '475-cleanup'], 'файл уходит как есть'
    assert len(sent) == n + 1 and 'details' not in sent[-1], 'отчёт minion2-nas идёт своим чередом, без итога'


def test_relay_only_when_file_changed():
    app, r = _relayer('r2', path=os.path.join(TMP, 'r2.json'))
    with open(r.relay_file, 'w', encoding='utf-8') as f:
        f.write('{"v": 1, "packages": []}')
    n = len(relayed)
    assert r.relay() is True and r.relay() is False and len(relayed) == n + 1
    with open(r.relay_file, 'w', encoding='utf-8') as f:
        f.write('{"v": 1, "packages": [{"id": "1-x"}]}')
    assert r.relay() is True and len(relayed) == n + 2


def test_relay_ignores_generated_at():
    """Движок пересобирает итог при каждом старте с новым generated_at — это не новый итог, строки истории нет."""
    _, r = _relayer('r5', path=os.path.join(TMP, 'r5.json'))
    for at in ('2026-11-26T12:00:00Z', '2026-11-26T13:00:00Z'):
        with open(r.relay_file, 'w', encoding='utf-8') as f:
            f.write('{"v": 1, "generated_at": "%s", "packages": []}' % at)
        n = len(relayed)
        r.relay()
    assert len(relayed) == n, 'второй файл отличается только generated_at — не шлём'
    with open(r.relay_file, 'w', encoding='utf-8') as f:
        f.write('{"v": 1, "generated_at": "2026-11-26T14:00:00Z", "packages": [{"id": "1-x"}]}')
    assert r.relay() is True


def test_relay_retries_after_refusal():
    _, r = _relayer('r3')
    codes.extend([0])
    assert r.relay() is False, 'сети нет — отпечаток не пишем'
    assert r.relay() is True


def test_relay_refuses_non_object_and_too_long():
    import io
    import contextlib
    path = os.path.join(TMP, 'r4.json')
    for content, says in (('[1]', 'не объект'), ('{"x": "%s"}' % ('a' * DETAILS_LIMIT), 'длиннее')):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        _, r = _relayer('r4', path=path)
        n = len(relayed)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert r.relay() is False
        assert len(relayed) == n and says in out.getvalue()


def test_relay_needs_both_keys_and_is_silent_in_dry_run():
    app = FakeApp('r5')
    r = reporter(app, relay_file=TGEDIT)
    assert r.relay_file == '' and r.relay() is False
    dry = Reporter(app, Section(url='u', key='k', relay_file=TGEDIT, relay_url=RELAY_URL), dry=True, send=fake_send)
    assert dry.relay() is False


def test_relay_after_stop_too():
    _, r = _relayer('r6')
    n = len(relayed)
    r.stopped(Stop(0, 'ждём'), 0)
    assert len(relayed) == n + 1


# --- outbox: общий канал контейнер → сайт (№170)
REPORT_URL = 'https://daetotyoma.com/admin/versions/minion2-nas'


def _outbox(name):
    """Приложение, папка outbox и отчётчик с одной строкой outbox_dir в [report]."""
    app = FakeApp(name)
    box = os.path.join(TMP, name + '-outbox')
    os.makedirs(box, exist_ok=True)
    return app, box, reporter(app, outbox_dir=box)


def _put(box, kind, body):
    import json
    with open(os.path.join(box, kind + '.json'), 'w', encoding='utf-8') as f:
        f.write(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))


def _sent_to(kind):
    return [body for url, body in relayed if url == '%s/%s' % (REPORT_URL, kind)]


def test_outbox_any_new_kind_goes_to_its_url_without_conf_change():
    """Новый вид — только новый файл в папке: conf и код nas-update те же."""
    _, box, r = _outbox('o1')
    _put(box, 'tgedit-report', {'v': 1, 'generated_at': '2026-11-26T14:00:00Z', 'packages': []})
    _put(box, 'some-future-kind', {'v': 1, 'n': 7})
    assert r.outbox() == 2
    future = _sent_to('some-future-kind')[-1]
    assert future['body'] == {'v': 1, 'n': 7} and future['host'].startswith('nas-') and future['sent_at'].endswith('Z')
    assert _sent_to('tgedit-report')[-1]['body']['packages'] == []


def test_outbox_resends_only_when_meaning_changed():
    _, box, r = _outbox('o2')
    _put(box, 'tgedit-report', {'v': 1, 'generated_at': '2026-11-26T12:00:00Z', 'packages': []})
    assert r.outbox() == 1
    _put(box, 'tgedit-report', {'v': 1, 'generated_at': '2026-11-26T13:00:00Z', 'packages': []})
    assert r.outbox() == 0, 'другое только generated_at — не новость'
    _put(box, 'tgedit-report', {'v': 1, 'generated_at': '2026-11-26T13:00:00Z', 'packages': [{'id': '1-x'}]})
    assert r.outbox() == 1


def test_outbox_retries_network_but_not_refused_file():
    import io
    import contextlib
    app, box, r = _outbox('o3')
    _put(box, 'unknown-kind', {'v': 1})
    codes.extend([0])
    assert r.outbox() == 0 and r.outbox() == 1, 'сети нет — повтор при следующем запуске'
    _put(box, 'unknown-kind', {'v': 1, 'x': 1})
    codes.extend([422])
    r.last_reply = ''
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert r.outbox() == 0
    assert 'отверг (422)' in out.getvalue()
    n = len(_sent_to('unknown-kind'))
    assert r.outbox() == 0 and len(_sent_to('unknown-kind')) == n, 'тот же отвергнутый файл не шлём каждый запуск'
    core.write_json(app.path('outbox-sent.json'), dict(core.read_json(app.path('outbox-sent.json'), {}), **{
        'unknown-kind': dict(core.read_json(app.path('outbox-sent.json'), {})['unknown-kind'], at=time.time() - OUTBOX_RETRY_SEC - 1)}))
    assert r.outbox() == 1, 'через сутки — снова (сайт мог научиться виду)'


def test_outbox_reads_only_regular_kind_files_of_a_real_dir():
    import io
    import contextlib
    app, box, r = _outbox('o4')
    secret = os.path.join(TMP, 'o4-secret.json')
    with open(secret, 'w', encoding='utf-8') as f:
        f.write('{"v": 1, "secret": true}')
    os.symlink(secret, os.path.join(box, 'linked.json'))
    _put(box, 'Bad_Name', {'v': 1})
    with open(os.path.join(box, 'tmpab12.part'), 'w', encoding='utf-8') as f:
        f.write('{"v": 1}')  # недописанный файл атомарной записи
    _put(box, 'no-version', {'packages': []})
    _put(box, 'not-object', '[1]')
    _put(box, 'too-long', {'v': 1, 'x': 'a' * OUTBOX_LIMIT})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert r.outbox() == 0
    said = out.getvalue()
    assert 'linked.json не шлю' in said and 'нет v: 1' in said and 'не объект' in said and 'длиннее' in said
    assert not any('secret' in str(body) for _, body in relayed)
    link = os.path.join(TMP, 'o4-link')
    os.symlink(box, link)
    with contextlib.redirect_stdout(io.StringIO()):
        assert reporter(app, outbox_dir=link).outbox() == 0, 'папка-ссылка не читается'
        assert reporter(app, outbox_dir=os.path.join(TMP, 'нет-такой')).outbox() == 0


def test_outbox_caps_files_per_run():
    _, box, r = _outbox('o5')
    for i in range(OUTBOX_FILES + 3):
        _put(box, 'k%02d' % i, {'v': 1})
    assert r.outbox() == OUTBOX_FILES


def test_outbox_silent_without_dir_or_in_dry_run_and_runs_after_stop():
    app, box, r = _outbox('o6')
    _put(box, 'tgedit-report', {'v': 1, 'packages': []})
    assert reporter(app).outbox() == 0
    assert Reporter(app, Section(url=REPORT_URL, key='k', outbox_dir=box), dry=True, send=fake_send).outbox() == 0
    n = len(_sent_to('tgedit-report'))
    r.stopped(Stop(0, 'ждём'), 0)
    assert len(_sent_to('tgedit-report')) == n + 1


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            try:
                fn()
                print('ok  ', name)
            except Exception as e:
                fails += 1
                print('FAIL', name, repr(e))
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
