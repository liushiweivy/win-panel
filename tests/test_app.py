#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
win-panel 单元测试：dispatch 全走注入替身，绝不触发真实关机/启动。
运行（项目根目录）：
  C:\\Users\\4v\\AppData\\Local\\Programs\\Python\\Python312\\python.exe -m unittest -v tests.test_app
"""
import contextlib
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

SD = app.sys32('shutdown.exe')
CMD = app.sys32('cmd.exe')


class FakeRunner:
    """记录所有 run(argv) 调用，按 exe 基名返回预设 rc；可加延迟制造并发交错。"""
    def __init__(self, codes=None, err='', delay=0.0):
        self.calls = []
        self.codes = codes or {}
        self.err = err
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(self, argv):
        with self._lock:
            self.calls.append(list(argv))
        if self.delay:
            time.sleep(self.delay)
        exe = os.path.basename(argv[0]).lower()
        return (self.codes.get(exe, 0), '', self.err if exe in self.codes else '')


class FakeLauncher:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, new_console=False):
        self.calls.append((list(argv), new_console))
        return None


class DispatchTest(unittest.TestCase):
    def setUp(self):
        app.PENDING.update(kind=None, until_ms=0, total_ms=0)
        self.run = FakeRunner()
        self.launch = FakeLauncher()
        self.uris = []
        self.bin_codes = []
        self.hk = []
        # 链接存储重定向到临时目录，绝不碰真实 links.json
        self.tmp = tempfile.mkdtemp(prefix='winpanel-test-')
        self._old_links_file = app.LINKS_FILE
        self._old_links = ({'folders': list(app.LINKS['folders']),
                            'sites': list(app.LINKS['sites'])})
        app.LINKS_FILE = os.path.join(self.tmp, 'links.json')
        app.LINKS['folders'] = []
        app.LINKS['sites'] = []

    def tearDown(self):
        app.LINKS_FILE = self._old_links_file
        app.LINKS['folders'] = self._old_links['folders']
        app.LINKS['sites'] = self._old_links['sites']
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, action, params=None, **fakes):
        return app.dispatch(
            action, params or {},
            run=fakes.get('run', self.run),
            launch=fakes.get('launch', self.launch),
            start_uri=fakes.get('start_uri', self.uris.append),
            empty_bin=fakes.get('empty_bin', lambda: self.bin_codes.pop(0)),
            hotkey=fakes.get('hotkey', lambda *a: self.hk.append(a)),
        )

    # ---- 分钟校验 ----
    def test_validate_minutes(self):
        self.assertEqual(app.validate_minutes('30'), 30)
        self.assertEqual(app.validate_minutes(1), 1)
        self.assertEqual(app.validate_minutes(1440), 1440)
        for bad in (0, -1, 1441, 'abc', None, 2.5, '  '):
            with self.assertRaises(ValueError):
                app.validate_minutes(bad)

    # ---- 定时 ----
    def test_schedule_shutdown_30_min(self):
        r = self.call('schedule', {'kind': 'shutdown', 'minutes': 30})
        self.assertTrue(r['ok'], r)
        self.assertIn('30', r['message'])
        self.assertEqual(self.run.calls[0][:2], [SD, '/a'])
        self.assertEqual(self.run.calls[1], [SD, '/s', '/t', '1800'])
        self.assertEqual(app.PENDING['kind'], 'shutdown')
        self.assertEqual(app.PENDING['total_ms'], 30 * 60000)
        self.assertIsNotNone(r['pending'])
        self.assertGreater(r['pending']['remain_ms'], 1790 * 1000)

    def test_schedule_restart_defaults_and_kind(self):
        r = self.call('schedule', {'kind': 'restart', 'minutes': 45})
        self.assertTrue(r['ok'])
        self.assertEqual(self.run.calls[1], [SD, '/r', '/t', '2700'])
        self.assertEqual(app.PENDING['kind'], 'restart')

    def test_schedule_unknown_kind_falls_back_shutdown(self):
        self.call('schedule', {'kind': 'rm -rf /', 'minutes': 1})
        self.assertEqual(self.run.calls[1][1], '/s')

    def test_schedule_rejects_bad_minutes_without_system_call(self):
        r = self.call('schedule', {'minutes': 99999})
        self.assertFalse(r['ok'])
        self.assertEqual(self.run.calls, [])  # 校验在前，不碰系统
        self.assertIsNone(app.PENDING['kind'])

    def test_schedule_failure_leaves_no_pending(self):
        run = FakeRunner(codes={'shutdown.exe': 1}, err='拒绝访问')
        r = self.call('schedule', {'minutes': 10}, run=run)
        self.assertFalse(r['ok'])
        self.assertIn('拒绝访问', r['message'])
        self.assertIsNone(app.PENDING['kind'])

    def test_concurrent_schedule_pending_stays_consistent(self):
        """8 线程并发计划：total_ms 必须始终等于 until-now，不允许撕裂组合。"""
        run = FakeRunner(delay=0.002)
        errors = []

        def worker(m):
            try:
                self.call('schedule', {'minutes': m}, run=run)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        ts = [threading.Thread(target=worker, args=(m,))
              for m in (5, 10, 7, 20, 90, 1, 30, 120)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(errors, [])
        with app.PENDING_LOCK:
            total = app.PENDING['total_ms']
            remain = app.PENDING['until_ms'] - int(time.time() * 1000)
        self.assertGreater(total, 0)
        self.assertLess(abs(remain - total), 2000)  # 计划执行耗时误差

    # ---- 取消 ----
    def test_cancel_success(self):
        r = self.call('cancel')
        self.assertTrue(r['ok'])
        self.assertEqual(self.run.calls[-1], [SD, '/a'])

    def test_cancel_none_scheduled_is_still_ok(self):
        run = FakeRunner(codes={'shutdown.exe': 1116})
        r = self.call('cancel', run=run)
        self.assertTrue(r['ok'])
        self.assertIn('没有', r['message'])

    def test_cancel_other_error(self):
        run = FakeRunner(codes={'shutdown.exe': 5})
        r = self.call('cancel', run=run)
        self.assertFalse(r['ok'])

    # ---- 电源 ----
    def test_destructive_power_needs_confirm(self):
        for act in ('shutdown_now', 'restart_now', 'logoff'):
            r = self.call(act)
            self.assertTrue(r['needConfirm'], act)
            self.assertFalse(r['ok'])
        self.assertEqual(self.run.calls, [])  # 未确认不执行
        r = self.call('shutdown_now', {'confirmed': True})
        self.assertTrue(r['ok'])
        self.assertEqual(self.run.calls[-1], [SD, '/s', '/t', '0'])

    def test_safe_power_no_confirm(self):
        r = self.call('hibernate')
        self.assertTrue(r['ok'])
        self.assertEqual(self.run.calls[-1], [SD, '/h'])
        r = self.call('lock')
        self.assertTrue(r['ok'])
        self.assertEqual(self.run.calls[-1][:2],
                         [app.sys32('rundll32.exe'), 'user32.dll,LockWorkStation'])

    def test_sleep_argv(self):
        self.call('sleep')
        self.assertEqual(self.run.calls[-1][:2],
                         [app.sys32('rundll32.exe'), 'powrprof.dll,SetSuspendState'])

    def test_power_failure_reported(self):
        run = FakeRunner(codes={'shutdown.exe': 1})
        r = self.call('restart_now', {'confirmed': True}, run=run)
        self.assertFalse(r['ok'])

    # ---- 快捷启动 ----
    def test_launch_argv(self):
        r = self.call('taskmgr')
        self.assertTrue(r['ok'])
        self.assertEqual(self.launch.calls, [([app.sys32('Taskmgr.exe')], False)])
        self.assertEqual(self.uris, [])

    def test_console_apps_get_new_console_flag(self):
        self.call('cmd')
        self.assertTrue(self.launch.calls[-1][1])
        self.assertEqual(self.launch.calls[-1][0][0], CMD)
        self.call('powershell')
        self.assertTrue(self.launch.calls[-1][1])
        # 回归：argv[0] 必须是完整 exe 路径，不能是目录（曾把 WindowsPowerShell 目录当程序名，WinError 5）
        self.assertEqual(self.launch.calls[-1][0][0], app.PS_EXE)
        # GUI 程序不带该标志
        self.call('notepad')
        self.assertFalse(self.launch.calls[-1][1])

    def test_all_launch_exe_paths_exist(self):
        """LAUNCH 里每个 argv[0] 都必须是真实存在的文件（URI 项除外）。"""
        for key, (spec, _label) in app.LAUNCH.items():
            if isinstance(spec, tuple):
                continue
            self.assertTrue(os.path.isfile(spec[0]), '%s -> %s' % (key, spec[0]))

    def test_launch_uri(self):
        r = self.call('settings')
        self.assertTrue(r['ok'])
        self.assertEqual(self.uris, ['ms-settings:'])
        self.assertEqual(self.launch.calls, [])
        self.call('snip')
        self.assertEqual(self.uris, ['ms-settings:', 'ms-screenclip:'])

    def test_launch_explorer_shell_folder(self):
        self.call('downloads')
        self.assertEqual(self.launch.calls[-1][0],
                         [app.EXPLORER, 'shell:Downloads'])

    def test_explorer_path_is_windows_root_not_system32(self):
        # 回归：explorer.exe 在 C:\Windows 下，System32 里没有（曾致文件夹三键全灭）
        self.assertTrue(os.path.isfile(app.EXPLORER))
        self.assertNotEqual(os.path.dirname(app.EXPLORER), app.SYS32)
        for key in ('thispc', 'downloads', 'documents'):
            self.assertEqual(app.LAUNCH[key][0][0], app.EXPLORER)

    # ---- Win 快捷键 ----
    def test_hotkey_dispatch_sends_whitelisted_keys(self):
        r = self.call('win_d')
        self.assertTrue(r['ok'])
        self.assertEqual(self.hk, [((0x5B,), 0x44, 0)])
        self.assertEqual(self.run.calls, [])  # 快捷键不走子进程

    def test_hotkey_multi_modifier_and_hold(self):
        self.call('win_shift_m')
        self.assertEqual(self.hk[-1], ((0x5B, 0xA0), 0x4D, 0))
        self.call('gamerec')
        self.assertEqual(self.hk[-1], ((0x5B, 0xA4), 0x52, 0))
        self.call('win_peek')
        self.assertEqual(self.hk[-1], ((0x5B,), 0xBC, 600))

    def test_hotkey_table_sanity(self):
        for name, (mods, vk, hold, label, keys) in app.HOTKEYS.items():
            self.assertTrue(all(m in app._ALLOWED_MODS for m in mods), name)
            self.assertGreaterEqual(vk, 0x08, name)
            self.assertLessEqual(vk, 0xFE, name)
            self.assertGreaterEqual(hold, 0, name)
            self.assertTrue(label and keys, name)
        # 每个 HOTKEYS 键都能路由成功
        for name in app.HOTKEYS:
            self.assertTrue(self.call(name)['ok'], name)

    def test_hotkey_ignores_attacker_params(self):
        # params 里的伪造键码不得影响固定白名单按键
        r = self.call('win_d', {'vk': 0x2E, 'mods': [0x11], 'hold_ms': 99999})
        self.assertTrue(r['ok'])
        self.assertEqual(self.hk, [((0x5B,), 0x44, 0)])

    def test_hotkeys_ui_in_sync(self):
        """index.html 的 data-hk 集合必须与 HOTKEYS 完全一致（前端按钮是静态写的）。"""
        import re
        with open(os.path.join(os.path.dirname(app.__file__), 'index.html'),
                  encoding='utf-8') as f:
            html = f.read()
        ui_keys = set(re.findall(r'data-hk="([^"]+)"', html))
        self.assertEqual(ui_keys, set(app.HOTKEYS))

    def test_launch_ui_in_sync(self):
        """静态快捷启动按钮（data-launch）必须与 LAUNCH 白名单一致。"""
        import re
        with open(os.path.join(os.path.dirname(app.__file__), 'index.html'),
                  encoding='utf-8') as f:
            html = f.read()
        ui_keys = set(re.findall(r'data-launch="([^"]+)"', html))
        self.assertEqual(ui_keys, set(app.LAUNCH))

    # ---- 自定义链接（文件夹 / 网站） ----
    def test_add_folder_requires_existing_dir(self):
        r = self.call('add_link', {'kind': 'folders', 'name': 'x',
                                   'target': r'E:\definitely\not\exist\12345'})
        self.assertFalse(r['ok'])
        self.assertIn('不存在', r['message'])
        self.assertEqual(app.LINKS['folders'], [])

    def test_add_folder_ok_and_persisted(self):
        r = self.call('add_link', {'kind': 'folders', 'name': '临时', 'target': self.tmp})
        self.assertTrue(r['ok'], r)
        self.assertEqual(app.LINKS['folders'][0]['name'], '临时')
        self.assertTrue(os.path.isfile(app.LINKS_FILE))  # 已落盘
        # 重新加载（模拟重启）仍在
        app.LINKS['folders'] = []
        app.load_links()
        self.assertEqual(app.LINKS['folders'][0]['path'], os.path.abspath(self.tmp))

    def test_add_site_scheme_whitelist(self):
        for bad in ('javascript:alert(1)', 'file:///C:/Windows/System32/cmd.exe',
                    'ms-settings:', 'ftp://x.com', 'notaurl', ''):
            r = self.call('add_link', {'kind': 'sites', 'name': 'bad', 'target': bad})
            self.assertFalse(r['ok'], bad)
        self.assertEqual(app.LINKS['sites'], [])
        r = self.call('add_link', {'kind': 'sites', 'name': 'ok', 'target': 'https://a.b/c'})
        self.assertTrue(r['ok'])

    def test_add_link_dedupe_and_limits(self):
        self.call('add_link', {'kind': 'sites', 'name': 'A', 'target': 'https://a.com'})
        r = self.call('add_link', {'kind': 'sites', 'name': 'a', 'target': 'https://b.com'})
        self.assertFalse(r['ok'])  # 大小写不敏感去重
        r = self.call('add_link', {'kind': 'sites', 'name': 'x' * 21, 'target': 'https://b.com'})
        self.assertFalse(r['ok'])  # 名称超长
        r = self.call('add_link', {'kind': 'wat', 'name': 'q', 'target': 'https://b.com'})
        self.assertFalse(r['ok'])  # 非法 kind
        for i in range(app.MAX_LINKS_PER_KIND - 1):  # 已有 1 条，补满
            self.call('add_link', {'kind': 'sites', 'name': 'n%d' % i, 'target': 'https://x.com'})
        r = self.call('add_link', {'kind': 'sites', 'name': 'over', 'target': 'https://x.com'})
        self.assertFalse(r['ok'])  # 超上限

    def test_del_link(self):
        self.call('add_link', {'kind': 'sites', 'name': 'A', 'target': 'https://a.com'})
        r = self.call('del_link', {'kind': 'sites', 'name': 'A'})
        self.assertTrue(r['ok'])
        self.assertEqual(app.LINKS['sites'], [])
        r = self.call('del_link', {'kind': 'sites', 'name': 'A'})
        self.assertFalse(r['ok'])

    def test_open_link_uses_stored_entry_only(self):
        # 不存在的名字：不碰系统
        r = self.call('open_folder', {'name': r'C:\Windows'})
        self.assertFalse(r['ok'])
        self.assertEqual(self.launch.calls, [])
        # 已存条目才能开
        self.call('add_link', {'kind': 'folders', 'name': '临时', 'target': self.tmp})
        r = self.call('open_folder', {'name': '临时'})
        self.assertTrue(r['ok'])
        self.assertEqual(self.launch.calls[-1][0], [app.EXPLORER, os.path.abspath(self.tmp)])
        self.call('add_link', {'kind': 'sites', 'name': 'G', 'target': 'https://github.com'})
        r = self.call('open_site', {'name': 'G'})
        self.assertTrue(r['ok'])
        self.assertEqual(self.uris, ['https://github.com'])

    def test_load_links_missing_and_corrupt(self):
        app.load_links()  # 文件不存在 → 播种默认并写盘
        self.assertTrue(os.path.isfile(app.LINKS_FILE))
        self.assertEqual(len(app.LINKS['sites']), len(app.DEFAULT_SITES))
        self.assertGreater(len(app.LINKS['folders']), 0)  # 本机桌面等真实存在
        for item in app.LINKS['folders']:
            self.assertTrue(os.path.isdir(item['path']), item)
        with open(app.LINKS_FILE, 'w', encoding='utf-8') as f:
            f.write('not-json{')
        app.load_links()  # 损坏 → 改名留底 + 回退默认
        self.assertTrue(os.path.isfile(app.LINKS_FILE + '.bad'))
        self.assertEqual(len(app.LINKS['sites']), len(app.DEFAULT_SITES))

    def test_load_links_non_dict_top_level_recovers(self):
        # 有效 JSON 但顶层不是对象（[1,2] / "x" / 123）：曾 AttributeError 崩启动
        for bad in ('[1,2]', '"x"', '123'):
            if os.path.exists(app.LINKS_FILE + '.bad'):
                os.remove(app.LINKS_FILE + '.bad')
            with open(app.LINKS_FILE, 'w', encoding='utf-8') as f:
                f.write(bad)
            app.load_links()
            self.assertEqual(len(app.LINKS['sites']), len(app.DEFAULT_SITES), bad)
            self.assertTrue(os.path.isfile(app.LINKS_FILE + '.bad'), bad)

    def test_load_links_drops_malformed_entries_keeps_good(self):
        with open(app.LINKS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'folders': [{'name': 'x'},            # 缺 path
                                   'just-a-string',           # 非 dict
                                   {'name': 'ok', 'path': self.tmp}],
                       'sites': [{'url': 'https://a.com'}]},  # 缺 name
                      f)
        app.load_links()
        self.assertEqual(app.LINKS['folders'], [{'name': 'ok', 'path': self.tmp}])
        self.assertEqual(app.LINKS['sites'], [])
        r = self.call('links')  # _links_view 不得再崩
        self.assertTrue(r['ok'])
        # 清理已回写
        with open(app.LINKS_FILE, encoding='utf-8') as f:
            saved = json.load(f)
        self.assertEqual(len(saved['folders']), 1)

    def test_del_and_open_case_insensitive(self):
        self.call('add_link', {'kind': 'sites', 'name': 'GitHub', 'target': 'https://github.com'})
        r = self.call('open_site', {'name': 'github'})
        self.assertTrue(r['ok'])
        r = self.call('del_link', {'kind': 'sites', 'name': 'GITHUB'})
        self.assertTrue(r['ok'])
        self.assertEqual(app.LINKS['sites'], [])

    # ---- 维护 ----
    def test_empty_bin_codes(self):
        self.bin_codes = [0]
        self.assertTrue(self.call('empty_bin')['ok'])
        self.bin_codes = [0x8000FFFF]
        self.assertTrue(self.call('empty_bin')['ok'])
        self.bin_codes = [0x80070005]
        self.assertFalse(self.call('empty_bin')['ok'])

    def test_clipboard_and_minimize_argv(self):
        self.call('clear_clipboard')
        self.assertEqual(self.run.calls[-1][:2], [CMD, '/c'])
        self.call('minimize_all')
        self.assertEqual(self.run.calls[-1][0], app.PS_EXE)
        self.assertIn('MinimizeAll', self.run.calls[-1][-1])

    # ---- 安全 ----
    def test_unknown_action_rejected(self):
        for evil in ('', 'format_c', 'launch_cmd_evil', '../shutdown', 'SCHEDULE'):
            r = self.call(evil, {'x': 1})
            self.assertFalse(r['ok'], evil)
        self.assertEqual(self.run.calls, [])
        self.assertEqual(self.launch.calls, [])

    def test_pending_view_expiry(self):
        app.PENDING.update(kind='shutdown', total_ms=60000,
                           until_ms=int(time.time() * 1000) + 60000)
        self.assertIsNotNone(app.pending_view())
        self.assertIsNone(app.pending_view(time.time() * 1000 + 61000))

    def test_log_message_suppresses_200_only(self):
        h = object.__new__(app.Handler)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            h.log_message('"%s" %s %s', 'GET /api/status HTTP/1.1', '200', '42')
            self.assertEqual(buf.getvalue(), '')
            h.log_message('"%s" %s %s', 'POST /api/action HTTP/1.1', '400', '12')
            self.assertIn('400', buf.getvalue())


class HttpTest(unittest.TestCase):
    """只打安全端点；绝不 POST 会真正执行系统操作的动作。"""

    @classmethod
    def setUpClass(cls):
        # 链接存储重定向到临时目录，HTTP 层的 add/del 不碰真实 links.json
        cls.tmp = tempfile.mkdtemp(prefix='winpanel-http-')
        cls._old_links_file = app.LINKS_FILE
        cls._old_links = ({'folders': list(app.LINKS['folders']),
                           'sites': list(app.LINKS['sites'])})
        app.LINKS_FILE = os.path.join(cls.tmp, 'links.json')
        app.LINKS['folders'] = []
        app.LINKS['sites'] = []
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        with urllib.request.urlopen('http://127.0.0.1:%d/api/token' % cls.port, timeout=5) as r:
            cls.token = json.loads(r.read().decode())['token']

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        app.LINKS_FILE = cls._old_links_file
        app.LINKS['folders'] = cls._old_links['folders']
        app.LINKS['sites'] = cls._old_links['sites']
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen('http://127.0.0.1:%d%s' % (self.port, path), timeout=5) as r:
            return r.status, r.read().decode('utf-8')

    def post_raw(self, body, ctype='application/json', host=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Content-Type': ctype}
        if host:
            headers['Host'] = host
        conn.request('POST', '/api/action', body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        return resp.status, (json.loads(data) if data else None)

    def post(self, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode('utf-8')
        return self.post_raw(data)

    def post_action(self, action, params=None, token=None):
        return self.post({'action': action, 'params': params or {},
                          'token': self.token if token is None else token})

    def test_index_served(self):
        code, body = self.get('/')
        self.assertEqual(code, 200)
        self.assertIn('系统控制台', body)

    def test_token_endpoint(self):
        self.assertEqual(len(self.token), 64)

    def test_status_shape(self):
        code, body = self.get('/api/status')
        data = json.loads(body)
        self.assertEqual(code, 200)
        for k in ('host', 'user', 'now_ms', 'uptime_ms', 'pending'):
            self.assertIn(k, data)
        self.assertGreater(data['uptime_ms'], 0)

    def test_actions_meta(self):
        code, body = self.get('/api/actions')
        data = json.loads(body)
        self.assertEqual(data['maxMin'], 1440)
        self.assertIn('shutdown_now', data['power'])
        self.assertEqual(data['launch']['settings'], '设置')

    def test_unknown_action_400_with_token(self):
        code, data = self.post_action('evil')
        self.assertEqual(code, 400)
        self.assertFalse(data['ok'])

    # ---- 自定义链接 HTTP 层 ----
    def test_links_get_and_roundtrip(self):
        code, body = self.get('/api/links')
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertIn('folders', data)
        self.assertIn('sites', data)
        # 加 → 列表出现 → 删 → 列表消失（全程不写真实 links.json）
        code, r = self.post_action('add_link', {'kind': 'sites', 'name': 'T',
                                                'target': 'https://t.example'})
        self.assertEqual(code, 200)
        _, body = self.get('/api/links')
        self.assertIn('T', [x['name'] for x in json.loads(body)['sites']])
        code, r = self.post_action('del_link', {'kind': 'sites', 'name': 'T'})
        self.assertEqual(code, 200)
        _, body = self.get('/api/links')
        self.assertNotIn('T', [x['name'] for x in json.loads(body)['sites']])

    def test_add_link_bad_scheme_400_over_http(self):
        code, data = self.post_action('add_link', {'kind': 'sites', 'name': 'x',
                                                   'target': 'javascript:alert(1)'})
        self.assertEqual(code, 400)
        self.assertFalse(data['ok'])

    def test_open_link_unknown_name_400(self):
        code, data = self.post_action('open_site', {'name': 'never-added'})
        self.assertEqual(code, 400)
        self.assertFalse(data['ok'])

    # ---- 跨站防御 ----
    def test_missing_token_403(self):
        code, data = self.post({'action': 'status', 'params': {}})  # 无 token
        self.assertEqual(code, 403)

    def test_wrong_token_403(self):
        code, _ = self.post_action('status', token='deadbeef')
        self.assertEqual(code, 403)

    def test_text_plain_content_type_rejected(self):
        body = json.dumps({'action': 'empty_bin', 'params': {}, 'token': self.token})
        code, data = self.post_raw(body.encode(), ctype='text/plain')
        self.assertEqual(code, 415)
        self.assertFalse(data['ok'])

    def test_form_content_type_rejected(self):
        code, _ = self.post_raw(b'action=empty_bin&token=' + self.token.encode(),
                                ctype='application/x-www-form-urlencoded')
        self.assertEqual(code, 415)

    def test_bad_host_rejected(self):
        code, data = self.post_raw(json.dumps({'action': 'status'}).encode(),
                                   host='evil.example:8830')
        self.assertEqual(code, 403)
        # GET 同样挡 rebinding
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/api/status', headers={'Host': 'evil.example:8830'})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_bad_json_400(self):
        code, _ = self.post_raw(b'not-json{')
        self.assertEqual(code, 400)

    def test_non_dict_json_400(self):
        for body in (b'null', b'[1,2]', b'123', b'"x"'):
            code, data = self.post_raw(body)
            self.assertEqual(code, 400, body)
            self.assertIn('对象', data['message'])

    def test_oversized_body_413(self):
        code, _ = self.post_raw(b'{"x":"' + b'a' * (app.MAX_BODY + 10) + b'"}')
        self.assertEqual(code, 413)

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.get('/nope')

    def test_post_wrong_path_404(self):
        body = json.dumps({'token': self.token}).encode()
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('POST', '/evil', body=body,
                     headers={'Content-Type': 'application/json'})
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
