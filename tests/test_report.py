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
from report import DETAILS_LIMIT, Reporter, fit_details, catalog_summary, js_length  # noqa: E402

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


def fake_send(body):
    sent.append(body)
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
