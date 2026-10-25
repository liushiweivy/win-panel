#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
win-panel: 本地 Windows 操作面板（零依赖，仅监听 127.0.0.1:8830）

安全设计：
  - 只绑定 127.0.0.1，外部网络不可达；
  - 操作全部走服务端白名单（固定 argv / 固定 API 调用），
    网页不能传任意命令（无 shell 注入面）；
  - 定时参数服务端校验为 1..1440 分钟整数。
"""
import ctypes
import getpass
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = '127.0.0.1'
PORT = 8830
MAX_MIN = 1440     # 定时上限 24 小时
MAX_BODY = 65536   # POST 体上限，防慢速大包占线程
ROOT = os.path.dirname(os.path.abspath(__file__))

# 系统工具一律走绝对路径，不依赖 CWD/PATH 搜索顺序
SYS32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
# explorer.exe 在 Windows 根目录，不在 System32（WinError 2 的坑，勿改回 sys32）
EXPLORER = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'explorer.exe')


def sys32(name):
    return os.path.join(SYS32, name)


# CSRF 防御：仅接受 Host 为 loopback 的请求（挡 DNS rebinding），
# 且每个进程签发一次性随机令牌，页面同源 GET 取得后随 POST 回传。
TOKEN = secrets.token_hex(32)
# Host 只认 loopback 主机名（rebinding 伪造的是 evil.com 而非端口；端口随监听套接字）
ALLOWED_HOST_NAMES = ('127.0.0.1', 'localhost')

# 本服务自己计划的关机/重启（外部 CLI 计划的系统不提供查询，只能靠“取消”盲取消）
PENDING = {'kind': None, 'until_ms': 0, 'total_ms': 0}
PENDING_LOCK = threading.RLock()  # 计划流程内会重入（持锁调 pending_view）

# ---------- 操作白名单 ----------
# 立即类电源操作（fixed argv）
POWER = {
    'shutdown_now': ([sys32('shutdown.exe'), '/s', '/t', '0'], '立即关机'),
    'restart_now':  ([sys32('shutdown.exe'), '/r', '/t', '0'], '立即重启'),
    'logoff':       ([sys32('shutdown.exe'), '/l'], '注销'),
    'hibernate':    ([sys32('shutdown.exe'), '/h'], '休眠'),
    'sleep':        ([sys32('rundll32.exe'), 'powrprof.dll,SetSuspendState', '0', '0', '0'], '睡眠'),
    'lock':         ([sys32('rundll32.exe'), 'user32.dll,LockWorkStation'], '锁定'),
}
NEED_CONFIRM = {'shutdown_now', 'restart_now', 'logoff'}

PS_EXE = os.path.join(SYS32, 'WindowsPowerShell', 'v1.0', 'powershell.exe')
TEMP_DIR = os.environ.get('TEMP') or os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'Temp')

# 快捷启动（exe argv）/ (('uri', scheme))
# 三组共用此白名单：系统工具 / 常用文件夹（explorer argv）/ 常用网站（uri）。
# 前端按钮是 index.html 静态写的，增删须同步 data-launch（test_launch_ui_in_sync 守卫）。
LAUNCH = {
    'taskmgr':   ([sys32('Taskmgr.exe')], '任务管理器'),
    'settings':  (('uri', 'ms-settings:'), '设置'),
    'thispc':    ([EXPLORER, 'shell:MyComputerFolder'], '此电脑'),
    'downloads': ([EXPLORER, 'shell:Downloads'], '下载'),
    'documents': ([EXPLORER, 'shell:Personal'], '文档'),
    'control':   ([sys32('control.exe')], '控制面板'),
    'cmd':       ([sys32('cmd.exe')], '命令提示符'),
    'powershell': ([PS_EXE, '-NoExit'], 'PowerShell'),
    'notepad':   ([sys32('notepad.exe')], '记事本'),
    'calc':      ([sys32('calc.exe')], '计算器'),
    'snip':      (('uri', 'ms-screenclip:'), '截图'),
    'mstsc':     ([sys32('mstsc.exe')], '远程桌面'),
    # rundll32 sysdm.cpl,EditEnvironmentVariables 直接弹「环境变量」对话框，
    # 比 SystemPropertiesAdvanced 再点一层快
    'envvars':   ([sys32('rundll32.exe'), 'sysdm.cpl,EditEnvironmentVariables'], '环境变量'),
    # 裂变奶龙桌宠（自研单进程 tkinter，笑时喷出小奶龙；项目 E:\1workspace\nailong-pet）
    'nailong':   ([r'C:\Users\4v\AppData\Local\Programs\Python\Python311\pythonw.exe',
                   r'E:\1workspace\nailong-pet\nailong.py'], '裂变奶龙桌宠'),
}

# ---------- 用户自定义链接（文件夹 / 网站，links.json 持久化，网页可增删） ----------
# 安全：网页只能按「名称」打开已存条目（open_folder/open_site 查表），
# 不能直接传路径/URL 执行；添加时服务端强校验（文件夹必须真实存在、
# 网站只收 http/https，挡 javascript:/file: 等伪协议绕过）。
LINKS_FILE = os.path.join(ROOT, 'links.json')
LINKS_LOCK = threading.RLock()
LINKS = {'folders': [], 'sites': []}
MAX_LINKS_PER_KIND = 60
MAX_LINK_NAME = 20
MAX_LINK_TARGET = 300

# SHGetFolderPathW 的 CSIDL：桌面/视频/音乐/Startup/AppData/图片
#（图片/视频/音乐可能被用户重定向到别处，勿写死 %USERPROFILE% 拼路径）
CSIDL = {'desktop': 0x0000, 'videos': 0x000E, 'music': 0x000D,
         'startup': 0x0007, 'appdata': 0x001A, 'pictures': 0x0027}


def _known_folder(csidl):
    buf = ctypes.create_unicode_buffer(260)
    try:
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
            return buf.value
    except (OSError, AttributeError):
        pass
    return None


def _default_folders():
    items = [
        ('桌面', _known_folder(CSIDL['desktop'])),
        ('图片', _known_folder(CSIDL['pictures'])),
        ('视频', _known_folder(CSIDL['videos'])),
        ('音乐', _known_folder(CSIDL['music'])),
        ('AppData', _known_folder(CSIDL['appdata'])),
        ('开机启动项', _known_folder(CSIDL['startup'])),
        ('临时文件', TEMP_DIR),
        ('字体', os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'Fonts')),
    ]
    # 个人常用目录由用户在网页上自行添加（存 links.json），源码不落私人路径
    return [{'name': n, 'path': p} for n, p in items if p and os.path.isdir(p)]


DEFAULT_SITES = [
    {'name': 'GitHub', 'url': 'https://github.com'},
    {'name': '火山方舟', 'url': 'https://console.volcengine.com/ark'},
    {'name': 'Claude', 'url': 'https://claude.ai'},
    {'name': 'DeepSeek', 'url': 'https://chat.deepseek.com'},
    {'name': '知乎', 'url': 'https://www.zhihu.com'},
    {'name': 'B 站', 'url': 'https://www.bilibili.com'},
]


def _seed_links():
    LINKS['folders'] = _default_folders()
    LINKS['sites'] = [dict(x) for x in DEFAULT_SITES]


def _clean_entries(items, field):
    """逐条校验：必须是 dict 且 name/目标字段为 str，畸形条目丢弃（保好留差）。"""
    good = [x for x in items if isinstance(x, dict)
            and isinstance(x.get('name'), str) and isinstance(x.get(field), str)]
    return good, len(items) - len(good)


def load_links(path=None):
    """启动时加载；文件缺失→写入默认；文件损坏→改名留底并回退默认。"""
    path = path or LINKS_FILE
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if (not isinstance(data, dict)
                or not isinstance(data.get('folders'), list)
                or not isinstance(data.get('sites'), list)):
            raise ValueError('bad shape')
        folders, dropped_f = _clean_entries(data['folders'], 'path')
        sites, dropped_s = _clean_entries(data['sites'], 'url')
        LINKS['folders'] = folders
        LINKS['sites'] = sites
        if dropped_f + dropped_s:
            sys.stderr.write('[links] 丢弃 %d 条畸形条目并回写清理\n' % (dropped_f + dropped_s))
            save_links(path)
    except FileNotFoundError:
        _seed_links()
        save_links(path)
    except (ValueError, OSError) as e:
        sys.stderr.write('[links] %s 损坏（%r），回退默认\n' % (path, e))
        try:
            os.replace(path, path + '.bad')
        except OSError:
            pass
        _seed_links()
        save_links(path)


def save_links(path=None):
    path = path or LINKS_FILE
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(LINKS, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # 同目录原子替换，避免写一半断电留坏文件

# ---------- Win 快捷键模拟（白名单固定按键，等价于亲手按组合键） ----------
# 虚拟键码
VK_LWIN, VK_LSHIFT, VK_LCTRL, VK_LMENU = 0x5B, 0xA0, 0xA2, 0xA4
KEYEVENTF_KEYUP = 0x0002
_ALLOWED_MODS = (VK_LWIN, VK_LSHIFT, VK_LCTRL, VK_LMENU)

# name: ((修饰键...), 主键, 按住毫秒, 名称, 键面文本)
# 注意：前端按钮是 index.html 静态写的，增删键时须同步 data-hk
# （tests.test_app.test_hotkeys_ui_in_sync 会强制对齐，改完跑测试即可发现漏同步）
HOTKEYS = {
    # 桌面 · 窗口
    'win_d':       ((VK_LWIN,), 0x44, 0, '显示桌面', 'Win+D'),
    'win_m':       ((VK_LWIN,), 0x4D, 0, '最小化全部窗口', 'Win+M'),
    'win_shift_m': ((VK_LWIN, VK_LSHIFT), 0x4D, 0, '还原窗口', 'Win+Shift+M'),
    'win_home':    ((VK_LWIN,), 0x24, 0, '仅留当前', 'Win+Home'),
    'win_peek':    ((VK_LWIN,), 0xBC, 600, '速览桌面（按住）', 'Win+,'),
    'win_tab':     ((VK_LWIN,), 0x09, 0, '任务视图', 'Win+Tab'),
    'win_z':       ((VK_LWIN,), 0x5A, 0, '窗口对齐布局', 'Win+Z'),
    'snap_left':   ((VK_LWIN,), 0x25, 0, '吸附到左半屏', 'Win+←'),
    'snap_right':  ((VK_LWIN,), 0x27, 0, '吸附到右半屏', 'Win+→'),
    'snap_up':     ((VK_LWIN,), 0x26, 0, '吸附最大化', 'Win+↑'),
    'snap_down':   ((VK_LWIN,), 0x28, 0, '吸附还原', 'Win+↓'),
    # 虚拟桌面
    'vdesk_new':   ((VK_LWIN, VK_LCTRL), 0x44, 0, '新建桌面', 'Win+Ctrl+D'),
    'vdesk_left':  ((VK_LWIN, VK_LCTRL), 0x25, 0, '左侧桌面', 'Win+Ctrl+←'),
    'vdesk_right': ((VK_LWIN, VK_LCTRL), 0x27, 0, '右侧桌面', 'Win+Ctrl+→'),
    'vdesk_close': ((VK_LWIN, VK_LCTRL), 0x73, 0, '关闭桌面', 'Win+Ctrl+F4'),
    # 面板 · 搜索
    'quick_settings': ((VK_LWIN,), 0x41, 0, '快速设置', 'Win+A'),
    'notif_center':   ((VK_LWIN,), 0x4E, 0, '通知中心', 'Win+N'),
    'widgets':        ((VK_LWIN,), 0x57, 0, '小组件', 'Win+W'),
    'winx':           ((VK_LWIN,), 0x58, 0, '高级菜单', 'Win+X'),
    'run_dialog':     ((VK_LWIN,), 0x52, 0, '运行对话框', 'Win+R'),
    'search':         ((VK_LWIN,), 0x53, 0, 'Windows 搜索', 'Win+S'),
    'feedback':       ((VK_LWIN,), 0x46, 0, '反馈中心', 'Win+F'),
    'sysinfo':        ((VK_LWIN,), 0x13, 0, '系统信息（关于）', 'Win+Pause'),
    # 输入 · 剪贴板
    'clip_history': ((VK_LWIN,), 0x56, 0, '剪贴板历史', 'Win+V'),
    'emoji':        ((VK_LWIN,), 0xBE, 0, '表情符号面板', 'Win+.'),
    'ime_switch':   ((VK_LWIN,), 0x20, 0, '切换输入法', 'Win+Space'),
    'voice_type':   ((VK_LWIN,), 0x48, 0, '语音输入', 'Win+H'),
    # 屏幕 · 游戏
    'project':   ((VK_LWIN,), 0x50, 0, '投影模式', 'Win+P'),
    'cast':      ((VK_LWIN,), 0x4B, 0, '无线投屏', 'Win+K'),
    'shot_full': ((VK_LWIN,), 0x2C, 0, '全屏截图（存入图片库）', 'Win+PrtScn'),
    'gamebar':   ((VK_LWIN,), 0x47, 0, 'Xbox 游戏栏', 'Win+G'),
    'gamerec':   ((VK_LWIN, VK_LMENU), 0x52, 0, '录制开关', 'Win+Alt+R'),
    'magnifier': ((VK_LWIN,), 0xBB, 0, '放大镜', 'Win+='),
    'explorer_qa': ((VK_LWIN,), 0x45, 0, '资源管理器', 'Win+E'),
}

# ---------- 执行原语（测试可注入替身） ----------
def run_capture(argv):
    """同步执行，返回 (returncode, stdout, stderr)；输出按 GBK 容错解码。"""
    p = subprocess.run(argv, capture_output=True)
    return (p.returncode,
            p.stdout.decode('gbk', 'ignore').strip(),
            p.stderr.decode('gbk', 'ignore').strip())


def detached(argv, new_console=False):
    """启动独立进程，服务不等待。
    GUI 程序用 DETACHED_PROCESS；cmd/PowerShell 等控制台程序必须
    CREATE_NEW_CONSOLE，否则 detached 后没有窗口（用户等于没点开）。"""
    flags = 0x00000010 if new_console else 0x00000008  # CREATE_NEW_CONSOLE / DETACHED_PROCESS
    subprocess.Popen(argv, close_fds=True, creationflags=flags)


HOTKEY_LOCK = threading.Lock()  # keybd_event 状态全局共享，跨线程并发会交错出组合错乱/卡键


def send_hotkey(mods, vk, hold_ms=0):
    """按下修饰键→主键→（可选按住）→反向抬起，等价于亲手按组合键。
    keybd_event 走的是系统输入队列，Shell 的全局热键（Win+R 等）会正常响应。"""
    u = ctypes.windll.user32
    with HOTKEY_LOCK:
        for m in mods:
            u.keybd_event(m, 0, 0, 0)
        u.keybd_event(vk, 0, 0, 0)
        if hold_ms:
            time.sleep(hold_ms / 1000.0)
        u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        for m in reversed(mods):
            u.keybd_event(m, 0, KEYEVENTF_KEYUP, 0)


def empty_recycle_bin():
    # SHERB_NOCONFIRMATION(1) | NOPROGRESS(2) | NOSOUND(4)
    return ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x0007)


def schedule_argv(kind, secs):
    return [sys32('shutdown.exe'), '/r' if kind == 'restart' else '/s', '/t', str(secs)]


def validate_minutes(v):
    try:
        m = int(str(v).strip())
    except (TypeError, ValueError, AttributeError):
        raise ValueError('需要 1-%d 的整数分钟' % MAX_MIN)
    if not (1 <= m <= MAX_MIN):
        raise ValueError('分钟数必须在 1-%d 之间' % MAX_MIN)
    return m


# 需要控制台窗口的启动项（其余 LAUNCH 均为 GUI 程序）
CONSOLE_LAUNCH = {'cmd', 'powershell'}


def pending_view(now_ms=None):
    with PENDING_LOCK:
        if not PENDING['kind']:
            return None
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        remain = PENDING['until_ms'] - now_ms
        if remain <= 0:
            return None
        return {'kind': PENDING['kind'], 'remain_ms': remain, 'total_ms': PENDING['total_ms']}


def _do_schedule(params, run):
    kind = 'restart' if params.get('kind') == 'restart' else 'shutdown'
    try:
        minutes = validate_minutes(params.get('minutes'))
    except ValueError as e:
        return {'ok': False, 'message': str(e)}
    with PENDING_LOCK:  # /a + /t 与状态更新成组，避免并发请求让 OS 与内存状态错位
        run([sys32('shutdown.exe'), '/a'])  # 清旧计划；无计划时 rc=1116，忽略
        rc, _, err = run(schedule_argv(kind, minutes * 60))
        if rc != 0:
            return {'ok': False, 'message': err or ('计划失败（%s）' % rc)}
        PENDING['kind'] = kind
        PENDING['total_ms'] = minutes * 60000
        PENDING['until_ms'] = int(time.time() * 1000) + PENDING['total_ms']
        view = pending_view()
    label = '重启' if kind == 'restart' else '关机'
    return {'ok': True, 'message': '%s已计划：%d 分钟后' % (label, minutes), 'pending': view}


def _do_cancel(run):
    with PENDING_LOCK:
        rc, _, _ = run([sys32('shutdown.exe'), '/a'])
        PENDING.update(kind=None, until_ms=0, total_ms=0)
    if rc == 0:
        return {'ok': True, 'message': '已取消关机/重启计划'}
    if rc == 1116:  # ERROR_NO_SHUTDOWN_IN_PROGRESS
        return {'ok': True, 'message': '当前没有待执行的关机计划'}
    return {'ok': False, 'message': '取消失败（%s）' % rc}


def _do_power(action, params, run):
    argv, label = POWER[action]
    if action in NEED_CONFIRM and not params.get('confirmed'):
        return {'ok': False, 'needConfirm': True, 'message': '请二次确认：' + label}
    rc, _, err = run(argv)
    if rc != 0:
        return {'ok': False, 'message': err or ('%s失败（%s）' % (label, rc))}
    return {'ok': True, 'message': '已执行：' + label}


def _do_launch(action, launch, start_uri):
    spec, label = LAUNCH[action]
    if isinstance(spec, tuple) and spec and spec[0] == 'uri':
        start_uri(spec[1])
    else:
        launch(spec, new_console=action in CONSOLE_LAUNCH)
    return {'ok': True, 'message': '已启动：' + label}


def _do_maintenance(action, run, empty_bin):
    if action == 'empty_bin':
        rc = empty_bin()
        if rc == 0 or rc == 0x8000FFFF:  # 0 / 回收站已是空的常见码
            return {'ok': True, 'message': '回收站已清空'}
        return {'ok': False, 'message': '清空回收站失败（%s）' % rc}
    if action == 'clear_clipboard':
        rc, _, _ = run([sys32('cmd.exe'), '/c', 'echo off | clip'])
        return {'ok': rc == 0, 'message': '剪贴板已清空' if rc == 0 else '清空失败'}
    ps = '(New-Object -ComObject Shell.Application).MinimizeAll()'
    rc, _, _ = run([PS_EXE, '-NoProfile', '-Command', ps])
    return {'ok': rc == 0, 'message': '已显示桌面' if rc == 0 else '失败'}


def _do_hotkey(action, hotkey):
    mods, vk, hold_ms, label, _keys = HOTKEYS[action]
    hotkey(mods, vk, hold_ms)
    return {'ok': True, 'message': '已发送快捷键：' + label}


def _links_view():
    with LINKS_LOCK:
        return {'ok': True,
                'folders': [dict(x) for x in LINKS['folders']],
                'sites': [dict(x) for x in LINKS['sites']]}


def _check_link_name(v):
    name = str(v or '').strip()
    if not (1 <= len(name) <= MAX_LINK_NAME):
        raise ValueError('名称需 1-%d 字' % MAX_LINK_NAME)
    return name


def _do_add_link(params):
    kind = params.get('kind')
    if kind not in ('folders', 'sites'):
        return {'ok': False, 'message': 'kind 必须是 folders 或 sites'}
    try:
        name = _check_link_name(params.get('name'))
        target = str(params.get('target') or '').strip()
        if not (1 <= len(target) <= MAX_LINK_TARGET):
            raise ValueError('内容为空或过长')
        if kind == 'folders':
            target = os.path.abspath(target)
            if not os.path.isdir(target):
                raise ValueError('文件夹不存在：' + target)
        else:
            u = urlparse(target)
            if u.scheme.lower() not in ('http', 'https') or not u.netloc:
                # 只收 http/https：挡 javascript:/file:/ms-settings: 等伪协议绕过
                raise ValueError('网址必须是 http:// 或 https:// 开头')
        with LINKS_LOCK:
            items = LINKS[kind]
            if len(items) >= MAX_LINKS_PER_KIND:
                raise ValueError('最多 %d 条' % MAX_LINKS_PER_KIND)
            if any(x['name'].lower() == name.lower() for x in items):
                raise ValueError('名称已存在：' + name)
            items.append({'name': name,
                          'path' if kind == 'folders' else 'url': target})
            save_links()
    except ValueError as e:
        return {'ok': False, 'message': str(e)}
    return {'ok': True, 'message': '已添加：' + name}


def _do_del_link(params):
    kind = params.get('kind')
    if kind not in ('folders', 'sites'):
        return {'ok': False, 'message': 'kind 必须是 folders 或 sites'}
    name = str(params.get('name') or '').lower()  # 与 add 的大小写不敏感去重对称
    with LINKS_LOCK:
        items = LINKS[kind]
        kept = [x for x in items if x['name'].lower() != name]
        if len(kept) == len(items):
            return {'ok': False, 'message': '条目不存在：' + name}
        LINKS[kind] = kept
        save_links()
    return {'ok': True, 'message': '已删除：' + name}


def _do_open_link(kind, params, launch, start_uri):
    name = str(params.get('name') or '').lower()  # 与 add/del 同规则，大小写不敏感
    with LINKS_LOCK:
        hit = next((x for x in LINKS[kind] if x['name'].lower() == name), None)
    if not hit:
        return {'ok': False, 'message': '条目不存在（可能已被删除，刷新试试）'}
    if kind == 'folders':
        launch([EXPLORER, hit['path']])
    else:
        start_uri(hit['url'])
    return {'ok': True, 'message': '已打开：' + name}


def dispatch(action, params=None, run=None, launch=None, start_uri=None, empty_bin=None, hotkey=None):
    """白名单路由；所有外部依赖均可注入（单测用）。返回 JSON 可序列化 dict。"""
    params = params or {}
    run = run or run_capture
    launch = launch or detached
    start_uri = start_uri or os.startfile
    empty_bin = empty_bin or empty_recycle_bin
    hotkey = hotkey or send_hotkey

    if action == 'status':
        return {'ok': True, 'pending': pending_view()}
    if action == 'schedule':
        return _do_schedule(params, run)
    if action == 'cancel':
        return _do_cancel(run)
    if action in POWER:
        return _do_power(action, params, run)
    if action in LAUNCH:
        return _do_launch(action, launch, start_uri)
    if action in HOTKEYS:
        return _do_hotkey(action, hotkey)
    if action in ('empty_bin', 'clear_clipboard', 'minimize_all'):
        return _do_maintenance(action, run, empty_bin)
    if action == 'links':
        return _links_view()
    if action == 'add_link':
        return _do_add_link(params)
    if action == 'del_link':
        return _do_del_link(params)
    if action in ('open_folder', 'open_site'):
        return _do_open_link('folders' if action == 'open_folder' else 'sites',
                             params, launch, start_uri)
    return {'ok': False, 'message': '未知操作，已拒绝'}


def system_status():
    return {
        'host': socket.gethostname(),
        'user': getpass.getuser(),
        'now_ms': int(time.time() * 1000),
        'uptime_ms': ctypes.windll.kernel32.GetTickCount64(),
        'pending': pending_view(),
    }


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = 'win-panel/1.0'
    timeout = 10  # 慢连接占线程上限

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, message, extra=None):
        body = {'ok': False, 'message': message}
        if extra:
            body.update(extra)
        self._send(code, json.dumps(body, ensure_ascii=False))

    def _host_ok(self):
        # 只认 loopback 主机名，挡 DNS rebinding（evil.com:8830 -> 127.0.0.1）
        name = self.headers.get('Host', '').lower().split(':', 1)[0]
        return name in ALLOWED_HOST_NAMES

    def do_GET(self):
        if not self._host_ok():
            self._json(403, 'forbidden host')
            return
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            try:
                with open(os.path.join(ROOT, 'index.html'), 'rb') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
            except OSError:
                self._json(404, 'index.html 缺失')
        elif path == '/api/status':
            self._send(200, json.dumps(system_status()))
        elif path == '/api/actions':
            meta = {
                'power': {k: v[1] for k, v in POWER.items()},
                'launch': {k: v[1] for k, v in LAUNCH.items()},
                'maxMin': MAX_MIN,
            }
            self._send(200, json.dumps(meta, ensure_ascii=False))
        elif path == '/api/token':
            # 跨域页面读不到响应体（无 CORS 头）；Host 校验已挡 rebinding
            self._send(200, json.dumps({'token': TOKEN}))
        elif path == '/api/links':
            self._send(200, json.dumps(_links_view(), ensure_ascii=False))
        else:
            self._json(404, 'not found')

    def do_POST(self):
        if not self._host_ok():
            self._json(403, 'forbidden host')
            return
        if self.path.split('?', 1)[0] != '/api/action':
            self._json(404, 'not found')
            return
        # 只接受 application/json：text/plain / form-urlencoded / multipart
        # 这些浏览器“简单请求”会在此被挡，跨域页面无法不发预检直接驱动操作
        ctype = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if ctype != 'application/json':
            self._json(415, 'only application/json accepted')
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self._json(400, 'bad Content-Length')
            return
        if n <= 0 or n > MAX_BODY:
            self._json(413, 'bad body size')
            return
        try:
            payload = json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            self._json(400, '请求体不是合法 JSON')
            return
        if not isinstance(payload, dict):
            self._json(400, '请求体必须是 JSON 对象')
            return
        if not secrets.compare_digest(str(payload.get('token', '')), TOKEN):
            self._json(403, 'bad token')
            return
        action = payload.get('action', '')
        params = payload.get('params') or {}
        if not isinstance(params, dict):
            self._json(400, 'params 必须是对象')
            return
        try:
            result = dispatch(action, params)
        except Exception as e:  # 细节只进服务端日志，不外泄路径/堆栈
            sys.stderr.write('[%s] dispatch error: %r\n' % (time.strftime('%H:%M:%S'), e))
            self._json(500, '服务器内部错误')
            return
        self._send(200 if result.get('ok') or result.get('needConfirm') else 400,
                   json.dumps(result, ensure_ascii=False))

    def log_message(self, fmt, *args):
        # log_request 的 args[1] 才是状态码（args[0] 是请求行）
        if len(args) >= 2 and str(args[1]) == '200':
            return
        sys.stderr.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), fmt % args))


def main():
    load_links()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = 'http://%s:%d/' % (HOST, PORT)
    print('win-panel running: ' + url)
    print('localhost only; Ctrl+C to stop.')
    if '--no-browser' not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')


if __name__ == '__main__':
    main()
